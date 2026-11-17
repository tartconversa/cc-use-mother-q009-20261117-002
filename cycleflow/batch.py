import json

from .contracts import DomainError, fingerprint, required_text, stable_json, utc_now
from .ledger import EventLedger


class BatchReceiver:
    def __init__(self, store):
        self.store = store
        self.ledger = EventLedger(store)

    def submit(self, batch_id, events, *, stop_after=None):
        batch_id = required_text(batch_id, "batch_id")
        if not isinstance(events, list) or not events:
            raise DomainError("invalid_batch", "events must be nonempty list")
        if stop_after is not None and (type(stop_after) is not int or stop_after < 1):
            raise DomainError("invalid_limit", "stop_after must be positive integer")
        try:
            manifest = fingerprint(events)
        except (ValueError, TypeError):
            raise DomainError("invalid_batch", "events must be JSON values") from None
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO batches(batch_id,manifest,next_line,total_lines,state,updated_at) VALUES(?,?,0,?,'running',?)",
                    (batch_id, manifest, len(events), utc_now()))
                next_line = 0
            else:
                if row["manifest"] != manifest or row["total_lines"] != len(events):
                    raise DomainError("batch_conflict", "batch ID reused with different manifest", 409)
                next_line = row["next_line"]
        outcomes = []
        ending = len(events) if stop_after is None else min(len(events), next_line + stop_after)
        for line in range(next_line, ending):
            try:
                outcome = self.ledger.ingest(events[line])
            except DomainError as exc:
                outcome = {"state": "invalid", "error_code": exc.code, "error_text": str(exc)}
            outcomes.append({"line": line, "outcome": outcome})
            with self.store.transaction() as db:
                row = db.execute("SELECT next_line,manifest FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
                if row["manifest"] != manifest or row["next_line"] != line:
                    raise DomainError("batch_race", "batch cursor changed during processing", 409)
                next_value = line + 1
                db.execute("UPDATE batches SET next_line=?,state=?,updated_at=? WHERE batch_id=?",
                           (next_value, "completed" if next_value == len(events) else "running", utc_now(), batch_id))
        return {"batch_id": batch_id, "manifest": manifest, "next_line": ending,
                "total_lines": len(events), "completed": ending == len(events), "outcomes": outcomes}

    def status(self, batch_id):
        batch_id = required_text(batch_id, "batch_id")
        with self.store.read() as db:
            row = db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise DomainError("unknown_batch", "batch not found", 404)
            return dict(row)
