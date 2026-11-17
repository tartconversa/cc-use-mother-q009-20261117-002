import json
import sqlite3

from .contracts import DomainError, MissingDependency, fingerprint, keys, required_text, stable_json, timestamp, utc_now
from .design import DesignRules
from .execution import ExecutionRules
from .inventory import InventoryRules
from .measure import MeasurementRules


KINDS = {
    "design.release", "design.retire", "lot.receive", "lot.quarantine",
    "job.open", "job.stage_start", "job.stage_finish", "job.scrap",
    "plan.create", "test.record",
}


class EventLedger:
    def __init__(self, store):
        self.store = store

    def _validate(self, event):
        keys(event, {"source", "external_id", "kind", "occurred_at", "payload"})
        source = required_text(event["source"], "source")
        external_id = required_text(event["external_id"], "external_id")
        kind = required_text(event["kind"], "kind")
        if kind not in KINDS:
            raise DomainError("unknown_kind", f"unsupported event kind {kind}")
        occurred_at = timestamp(event["occurred_at"])
        if not isinstance(event["payload"], dict):
            raise DomainError("invalid_payload", "payload must be object")
        try:
            payload = json.loads(stable_json(event["payload"]))
        except (TypeError, ValueError):
            raise DomainError("invalid_payload", "payload must be JSON object") from None
        normalized = {"source": source, "external_id": external_id, "kind": kind,
                      "occurred_at": occurred_at, "payload": payload}
        return normalized, fingerprint(normalized)

    def ingest(self, event):
        normalized, digest = self._validate(event)
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM events WHERE source=? AND external_id=?",
                                  (normalized["source"], normalized["external_id"])).fetchone()
            if existing is not None:
                if existing["fingerprint"] != digest:
                    raise DomainError("identity_conflict", "event key reused with different content", 409)
                return self._result(existing, repeated=True)
            cursor = db.execute(
                "INSERT INTO events(source,external_id,kind,occurred_at,received_at,payload,fingerprint,state) VALUES(?,?,?,?,?,?,?,'pending')",
                (normalized["source"], normalized["external_id"], normalized["kind"],
                 normalized["occurred_at"], utc_now(), stable_json(normalized["payload"]), digest))
            event_id = cursor.lastrowid
            self._apply(db, event_id)
            return self._result(db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def _apply(self, db, event_id):
        row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        payload = json.loads(row["payload"])
        try:
            db.execute("SAVEPOINT project_event")
            kind = row["kind"]
            if kind.startswith("design."):
                detail = DesignRules(db).dispatch(kind, payload, row["occurred_at"])
            elif kind.startswith("lot."):
                detail = InventoryRules(db).dispatch(kind, payload, row["occurred_at"])
            elif kind.startswith("job."):
                detail = ExecutionRules(db).dispatch(kind, payload, row["occurred_at"], event_id)
            else:
                detail = MeasurementRules(db).dispatch(kind, payload, row["occurred_at"], event_id)
            db.execute("RELEASE SAVEPOINT project_event")
            db.execute("UPDATE events SET state='accepted',error_code=NULL,error_text=NULL,attempts=attempts+1 WHERE id=?",
                       (event_id,))
            return detail
        except (DomainError, sqlite3.IntegrityError) as exc:
            db.execute("ROLLBACK TO SAVEPOINT project_event")
            db.execute("RELEASE SAVEPOINT project_event")
            if isinstance(exc, DomainError):
                code, message = exc.code, str(exc)
            else:
                code, message = "constraint", str(exc)
            state = "waiting" if code in {"missing_dependency", "shortage"} else "rejected"
            db.execute("UPDATE events SET state=?,error_code=?,error_text=?,attempts=attempts+1 WHERE id=?",
                       (state, code, message, event_id))
            return None

    def retry(self, event_id):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise MissingDependency("event", str(event_id))
            if row["state"] != "waiting":
                raise DomainError("not_retryable", "only waiting dependency events can retry")
            self._apply(db, event_id)
            return self._result(db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def retry_waiting(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DomainError("invalid_limit", "limit outside 1..1000")
        with self.store.read() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM events WHERE state='waiting' ORDER BY id LIMIT ?",
                                                (limit,))]
        return [self.retry(event_id) for event_id in ids]

    def get(self, event_id):
        with self.store.read() as db:
            row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise MissingDependency("event", str(event_id))
            result = self._result(row)
            result["payload"] = json.loads(row["payload"])
            result["received_at"] = row["received_at"]
            return result

    def _result(self, row, repeated=False):
        return {"id": row["id"], "source": row["source"], "external_id": row["external_id"],
                "kind": row["kind"], "occurred_at": row["occurred_at"],
                "state": row["state"], "error_code": row["error_code"],
                "error_text": row["error_text"], "attempts": row["attempts"],
                "fingerprint": row["fingerprint"], "repeated": repeated}

    def problems(self, product=None):
        with self.store.read() as db:
            rows = db.execute("SELECT * FROM events WHERE state!='accepted' ORDER BY id").fetchall()
            return [self._result(row) for row in rows]

    def lineage(self, event_id):
        event = self.get(event_id)
        with self.store.read() as db:
            payload = event["payload"]
            if event["kind"] == "test.record" and event["state"] == "accepted":
                event["trace"] = MeasurementRules(db).trace(payload["test_id"])
            elif event["kind"].startswith("job.") and event["state"] == "accepted":
                event["trace"] = ExecutionRules(db).lineage(payload["job_id"])
            return event
