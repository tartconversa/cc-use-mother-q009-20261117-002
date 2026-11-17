from cycleflow.demo import END, START
from tests.common import ServiceCase


class JobMeasurementTests(ServiceCase):
    def test_workflow_reaches_accepted_quality_gate(self):
        report = self.full_scenario()
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        self.assertEqual(report["summary"]["passed_units"], 3)
        self.assertEqual(report["summary"]["pass_rate"], "0.75")
        self.assertEqual(report["summary"]["mean_cycle_seconds"], 1380)

    def test_stage_sequence_rejects_skipping_machining(self):
        self.seed_design()
        self.seed_lots()
        self.seed_job()
        outcome = self.ingest(20, "job.stage_start", 5, {"job_id": "J1", "stage": "assembly"})
        self.assertEqual(outcome["error_code"], "stage_order")
        self.assertEqual(self.service.get_job("J1")["stages"], [])

    def test_stage_end_requires_start(self):
        self.seed_design()
        self.seed_lots()
        self.seed_job()
        outcome = self.ingest(20, "job.stage_finish", 6, {"job_id": "J1", "stage": "machining"})
        self.assertEqual(outcome["error_code"], "stage_not_started")
        self.assertEqual(self.service.get_job("J1")["status"], "open")

    def test_stage_end_before_start_rejected(self):
        self.seed_design()
        self.seed_lots()
        self.seed_job()
        self.ingest(6, "job.stage_start", 10, {"job_id": "J1", "stage": "machining"})
        outcome = self.ingest(7, "job.stage_finish", 9, {"job_id": "J1", "stage": "machining"})
        self.assertEqual(outcome["error_code"], "time_order")
        self.assertIsNone(self.service.get_job("J1")["stages"][0]["completed_at"])

    def test_test_requires_completed_integration(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        outcome = self.ingest(20, "test.record", 15, {"test_id": "T1", "job_id": "J1",
                                                       "plan_id": "P1", "passed": 4, "failed": 0})
        self.assertEqual(outcome["error_code"], "job_not_testable")
        self.assertEqual(self.service.get_job("J1")["status"], "open")

    def test_test_quantity_must_match_job(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self.complete_stages()
        outcome = self.ingest(20, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                                       "plan_id": "P1", "passed": 1, "failed": 1})
        self.assertEqual(outcome["error_code"], "sample_mismatch")
        self.assertEqual(self.service.get_job("J1")["status"], "awaiting_test")

    def test_quality_gate_failure_allows_rework(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self.complete_stages()
        failed = self.ingest(20, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                                       "plan_id": "P1", "passed": 2, "failed": 2})
        self.assertEqual(failed["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        child = self.seed_job("J2", quantity=2, number=21, minute=31, parent="J1")
        self.assertEqual(child["state"], "accepted")
        self.assertEqual(self.service.get_job("J2")["parent_job"], "J1")
        self.assertEqual(self.service.audit("robot-arm")["finding_count"], 0)

    def test_rework_disallowed_for_good_parent(self):
        self.full_scenario()
        outcome = self.seed_job("J2", quantity=1, number=21, minute=31, parent="J1")
        self.assertEqual(outcome["error_code"], "invalid_rework")

    def test_scrap_releases_reserved_components(self):
        self.seed_design()
        self.seed_lots()
        self.seed_job()
        before = self.service.get_lot("M1")["available"]
        outcome = self.ingest(20, "job.scrap", 10, {"job_id": "J1", "reason": "bad frame"})
        self.assertEqual(outcome["state"], "accepted")
        self.assertEqual(self.service.get_lot("M1")["available"], before + 8)
        self.assertEqual(self.service.get_job("J1")["reservations"], [])

    def test_timeline_explains_wait_and_stage_duration(self):
        self.full_scenario()
        view = self.service.job_timeline("J1")
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["lineage"][0]["job_id"], "J1")
        self.assertTrue(any(item["seconds"] == 60 for item in view["gaps"]))
        self.assertEqual(len(view["reservations"]), 2)

    def test_plan_revision_must_match_job(self):
        self.full_scenario()
        outcome = self.ingest(20, "plan.create", 31, {"plan_id": "P2", "product": "robot-arm",
                                                       "revision": 2, "minimum_pass_rate": "0.9"})
        self.assertEqual(outcome["error_code"], "missing_dependency")
