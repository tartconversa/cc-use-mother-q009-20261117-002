import json

from .analytics import CycleAnalytics, window
from .contracts import DomainError, MissingDependency, fingerprint, required_text, stable_json, utc_now


class ReportShelf:
    def __init__(self, store):
        self.store = store
        self.analytics = CycleAnalytics(store)

    def preview(self, product, start, end):
        return self.analytics.report(product, start, end)

    def publish(self, release_id, product, start, end, expected_fingerprint):
        release_id = required_text(release_id, "release_id")
        product = required_text(product, "product")
        expected_fingerprint = required_text(expected_fingerprint, "expected_fingerprint")
        start, end = window(start, end)
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            if existing is not None:
                if (existing["product"], existing["window_start"], existing["window_end"],
                    existing["fingerprint"]) == (product, start, end, expected_fingerprint):
                    return self._serialize(existing, repeated=True)
                raise DomainError("release_conflict", "release ID already bound to different content", 409)
            report = self.analytics.report(product, start, end)
            if report["fingerprint"] != expected_fingerprint:
                raise DomainError("stale_preview", "preview fingerprint no longer matches current facts", 409)
            if not report["quality"]["publishable"]:
                raise DomainError("quality_block", "unresolved quality issues prevent publication", 409)
            db.execute(
                "INSERT INTO releases(release_id,product,window_start,window_end,created_at,report,fingerprint,source_head) VALUES(?,?,?,?,?,?,?,?)",
                (release_id, product, start, end, utc_now(), stable_json(report), expected_fingerprint, report["source_head"]))
            row = db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            return self._serialize(row)

    def get(self, release_id):
        with self.store.read() as db:
            row = db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            if row is None:
                raise MissingDependency("release", release_id)
            return self._serialize(row)

    def _serialize(self, row, repeated=False):
        report = json.loads(row["report"])
        stored = report.pop("fingerprint", None)
        if fingerprint(report) != stored or stored != row["fingerprint"]:
            raise DomainError("corrupt_release", "stored report fingerprint is invalid", 500)
        report["fingerprint"] = stored
        return {"release_id": row["release_id"], "created_at": row["created_at"],
                "fingerprint": row["fingerprint"], "source_head": row["source_head"],
                "repeated": repeated, "report": report}

    def list_product(self, product):
        with self.store.read() as db:
            rows = db.execute("SELECT * FROM releases WHERE product=? ORDER BY created_at,release_id", (product,))
            return [self._serialize(row) for row in rows]

    def freshness(self, release_id):
        frozen = self.get(release_id)
        report = frozen["report"]
        current = self.preview(report["product"], report["window_start"], report["window_end"])
        return {"release_id": release_id, "frozen_fingerprint": frozen["fingerprint"],
                "current_fingerprint": current["fingerprint"],
                "current_differs": current["fingerprint"] != frozen["fingerprint"],
                "frozen_source_head": frozen["source_head"], "current_source_head": current["source_head"]}
