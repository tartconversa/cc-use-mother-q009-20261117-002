"""Reproducible comparable-cohort scenario for pilot ramp-up decisions.

Builds a two-week synthetic history where design revisions, supplier lots,
rework and test coverage all move at once, then runs the cohort engine twice:

* before a late supplemental test arrives (and freezes that view);
* after the late event is ingested, showing the drift against the freeze.

Run with ``python -m cycleflow.cohort_demo``.  Everything lives in a temporary
database and the output is deterministic JSON.
"""

import json
import tempfile
from pathlib import Path

from .service import CycleService


W1_START, W1_END = "2026-01-05T00:00:00Z", "2026-01-12T00:00:00Z"
W2_START, W2_END = "2026-01-12T00:00:00Z", "2026-01-19T00:00:00Z"


class ScenarioBuilder:
    def __init__(self, service, start=0):
        self.service = service
        self.n = start

    def add(self, kind, at, payload):
        self.n += 1
        return self.service.ingest({
            "source": "cohort-demo", "external_id": f"D{self.n:03d}",
            "kind": kind, "occurred_at": at, "payload": payload})

    def design(self, design_id, revision, parts, parent=None):
        payload = {"design_id": design_id, "product": "robot-arm",
                   "revision": revision, "parts": parts}
        if parent:
            payload["parent_id"] = parent
        return self.add("design.release", f"2026-01-01T0{revision}:00:00Z", payload)

    def lot(self, lot_id, part, supplier, qty):
        return self.add("lot.receive", "2026-01-02T08:00:00Z",
                        {"lot_id": lot_id, "part": part, "supplier": supplier,
                         "quantity": qty})

    def plan(self, plan_id, revision, threshold="0.50"):
        return self.add("plan.create", f"2026-01-03T0{revision}:00:00Z",
                        {"plan_id": plan_id, "product": "robot-arm",
                         "revision": revision, "minimum_pass_rate": threshold})

    def job(self, job_id, design_id, qty, day, passed, failed, plan_id, parent=None):
        payload = {"job_id": job_id, "design_id": design_id, "quantity": qty}
        if parent:
            payload["parent_job"] = parent
        opened = self.add("job.open", f"2026-01-{day:02d}T09:00:00Z", payload)
        assert opened["state"] == "accepted", opened
        for hour, stage in ((0, "machining"), (1, "assembly"), (2, "integration")):
            self.add("job.stage_start", f"2026-01-{day:02d}T1{hour}:00:00Z",
                     {"job_id": job_id, "stage": stage})
            self.add("job.stage_finish", f"2026-01-{day:02d}T1{hour}:30:00Z",
                     {"job_id": job_id, "stage": stage})
        return self.add("test.record", f"2026-01-{day:02d}T15:00:00Z",
                        {"test_id": f"T-{job_id}", "job_id": job_id,
                         "plan_id": plan_id, "passed": passed, "failed": failed})


def build_history(service):
    b = ScenarioBuilder(service)
    # Revision 1: standard config (motor).  Revision 2: premium config (servo).
    b.design("R1", 1, {"motor": 1, "sensor": 1})
    b.design("R2", 2, {"servo": 1, "sensor": 1}, parent="R1")
    b.lot("M-STD", "motor", "beta-motors", 200)
    b.lot("V-PREM", "servo", "alpha-motors", 200)
    b.lot("S-A", "sensor", "alpha-sensors", 400)
    b.plan("P1", 1)
    b.plan("P2", 2)
    # Week 1: standard-heavy mix.  Standard 24/40 (60%), premium 8/10 (80%).
    b.job("J-STD-W1", "R1", 40, 6, 24, 16, "P1")
    b.job("J-PRE-W1", "R2", 10, 7, 8, 2, "P2")
    # Week 2: premium-heavy mix.  Standard 7/10 (70%), premium 36/40 (90%).
    b.job("J-STD-W2", "R1", 10, 13, 7, 3, "P1")
    b.job("J-PRE-W2", "R2", 40, 14, 36, 4, "P2")
    return b


def run():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "cohort.sqlite3"
        service = CycleService(db)
        build_history(service)

        before = service.cohort_compare(
            "robot-arm", W1_START, W1_END, W2_START, W2_END)
        # Freeze the week-1 report so the early view stays reproducible.
        week1 = service.report("robot-arm", W1_START, W1_END)
        release = service.publish("week1-freeze", "robot-arm",
                                  W1_START, W1_END, week1["fingerprint"])

        # A late supplemental test for a job that was still open at freeze time.
        b = ScenarioBuilder(service, start=service.health()["event_head"])
        b.add("job.open", "2026-01-08T09:00:00Z",
              {"job_id": "J-LATE", "design_id": "R1", "quantity": 8})
        for hour, stage in ((0, "machining"), (1, "assembly"), (2, "integration")):
            b.add("job.stage_start", f"2026-01-08T1{hour}:00:00Z",
                  {"job_id": "J-LATE", "stage": stage})
            b.add("job.stage_finish", f"2026-01-08T1{hour}:30:00Z",
                  {"job_id": "J-LATE", "stage": stage})
        b.add("test.record", "2026-01-17T15:00:00Z",
              {"test_id": "T-J-LATE", "job_id": "J-LATE",
               "plan_id": "P1", "passed": 7, "failed": 1})

        after = service.cohort_compare(
            "robot-arm", W1_START, W1_END, W2_START, W2_END)
        drift = service.cohort_drift("week1-freeze")
        return {
            "frozen_release_id": release["release_id"],
            "frozen_fingerprint": release["fingerprint"],
            "before_late_event": {
                "verdict": before["interpretation"]["verdict"],
                "naive_rates": {
                    "first": before["naive_comparison"]["first"]["interval"]["rate"],
                    "second": before["naive_comparison"]["second"]["interval"]["rate"]},
                "matched_delta": before["matched_comparison"]["delta_interval"],
                "decomposition": {
                    "within_quality": before["decomposition"]["within_cohort_quality_effect"],
                    "configuration_mix": before["decomposition"]["configuration_mix_effect"],
                    "volume": before["decomposition"]["volume_effect"]}},
            "after_late_event": {
                "verdict": after["interpretation"]["verdict"],
                "matched_delta": after["matched_comparison"]["delta_interval"],
                "coverage_first": after["coverage"]["first"]},
            "drift": {
                "verdict_changed": drift["verdict_changed"],
                "jobs_added": drift["job_changes"]["added_count"],
                "frozen_head": drift["frozen_source_head"],
                "current_head": drift["current_source_head"],
                "frozen_release_unchanged":
                    service.get_release("week1-freeze")["fingerprint"] == release["fingerprint"]},
        }


def main():
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
