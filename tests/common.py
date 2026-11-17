import tempfile
import unittest
from pathlib import Path

from cycleflow.demo import END, START, event, scenario
from cycleflow.service import CycleService


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data.sqlite3"
        self.service = CycleService(self.path)

    def ingest(self, number, kind, minute, payload, source="test"):
        return self.service.ingest(event(number, kind, minute, payload, source))

    def seed_design(self):
        return self.ingest(1, "design.release", 0, {"design_id": "R1", "product": "robot-arm",
                                                     "revision": 1, "parts": {"motor": 2, "sensor": 1}})

    def seed_lots(self):
        self.ingest(2, "lot.receive", 1, {"lot_id": "M1", "part": "motor",
                                               "supplier": "motors", "quantity": 12})
        self.ingest(3, "lot.receive", 2, {"lot_id": "S1", "part": "sensor",
                                               "supplier": "sensors", "quantity": 6})

    def seed_plan(self):
        return self.ingest(4, "plan.create", 3, {"plan_id": "P1", "product": "robot-arm",
                                                  "revision": 1, "minimum_pass_rate": "0.75"})

    def seed_job(self, job_id="J1", quantity=4, number=5, minute=4, parent=None):
        payload = {"job_id": job_id, "design_id": "R1", "quantity": quantity}
        if parent:
            payload["parent_job"] = parent
        return self.ingest(number, "job.open", minute, payload)

    def complete_stages(self, job_id="J1", base=6):
        for offset, stage, minute in ((0, "machining", 5), (2, "assembly", 12), (4, "integration", 21)):
            self.ingest(base + offset, "job.stage_start", minute, {"job_id": job_id, "stage": stage})
            self.ingest(base + offset + 1, "job.stage_finish", minute + 4,
                        {"job_id": job_id, "stage": stage})

    def full_scenario(self):
        outcomes = [self.service.ingest(item) for item in scenario()]
        self.assertTrue(all(item["state"] == "accepted" for item in outcomes), outcomes)
        return self.service.report("robot-arm", START, END)
