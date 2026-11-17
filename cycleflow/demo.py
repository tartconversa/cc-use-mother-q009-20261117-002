import json
import tempfile
from pathlib import Path

from .audit import IntegrityAudit
from .service import CycleService


DAY = "2026-01-12"
START = "2026-01-01T00:00:00Z"
END = "2026-02-01T00:00:00Z"


def event(number, kind, minute, payload, source="demo"):
    return {"source": source, "external_id": str(number), "kind": kind,
            "occurred_at": f"{DAY}T09:{minute:02d}:00Z", "payload": payload}


def scenario():
    return [
        event(1, "design.release", 0, {"design_id": "R1", "product": "robot-arm",
                                       "revision": 1, "parts": {"motor": 2, "sensor": 1},
                                       "specification": {"load_kg": 5}}),
        event(2, "lot.receive", 1, {"lot_id": "M1", "part": "motor",
                                     "supplier": "local-motor", "quantity": 8}),
        event(3, "lot.receive", 2, {"lot_id": "S1", "part": "sensor",
                                     "supplier": "local-sensor", "quantity": 4}),
        event(4, "plan.create", 3, {"plan_id": "P1", "product": "robot-arm",
                                     "revision": 1, "minimum_pass_rate": "0.75"}),
        event(5, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 4}),
        event(6, "job.stage_start", 5, {"job_id": "J1", "stage": "machining"}),
        event(7, "job.stage_finish", 10, {"job_id": "J1", "stage": "machining"}),
        event(8, "job.stage_start", 12, {"job_id": "J1", "stage": "assembly"}),
        event(9, "job.stage_finish", 20, {"job_id": "J1", "stage": "assembly"}),
        event(10, "job.stage_start", 21, {"job_id": "J1", "stage": "integration"}),
        event(11, "job.stage_finish", 27, {"job_id": "J1", "stage": "integration"}),
        event(12, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                      "plan_id": "P1", "passed": 3, "failed": 1,
                                      "note": "Synthetic local fixture"}),
    ]


def run():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "cycleflow.sqlite3"
        service = CycleService(db)
        first = service.ingest_batch("sample", scenario(), stop_after=5)
        assert first["next_line"] == 5
        rest = service.ingest_batch("sample", scenario())
        assert rest["completed"]
        report = service.report("robot-arm", START, END)
        assert report["summary"]["passed_units"] == 3
        assert report["quality"]["publishable"]
        frozen = service.publish("jan-demo", "robot-arm", START, END, report["fingerprint"])
        assert frozen["report"]["summary"]["pass_rate"] == "0.75"
        audit = IntegrityAudit(service.store).run("robot-arm")
        assert audit["ok"], audit
        return {"batch": {"first_cursor": first["next_line"], "complete": rest["completed"]},
                "report": report["summary"], "quality": report["quality"],
                "release_id": frozen["release_id"], "audit": audit}


def main():
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
