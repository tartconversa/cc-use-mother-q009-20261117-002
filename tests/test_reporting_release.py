from cycleflow.contracts import DomainError
from cycleflow.demo import END, START, event
from tests.common import ServiceCase


class ReportingReleaseTests(ServiceCase):
    def test_report_reconciles_stages_wait_and_quality(self):
        report = self.full_scenario()
        self.assertEqual(report["summary"]["job_count"], 1)
        self.assertEqual(report["summary"]["mean_wait_seconds"], 240)
        self.assertEqual(report["revisions"][0]["stage_mean_seconds"]["machining"], 300)
        self.assertEqual(report["revisions"][0]["tested_units"], 4)
        self.assertEqual(report["jobs"][0]["cycle_seconds"], 1380)
        self.assertTrue(report["quality"]["publishable"])

    def test_unfinished_job_blocks_release(self):
        self.seed_design()
        self.seed_lots()
        self.seed_job()
        report = self.service.report("robot-arm", START, END)
        self.assertFalse(report["quality"]["publishable"])
        self.assertIn("unfinished_job", {item["code"] for item in report["quality"]["issues"]})
        with self.assertRaises(DomainError) as caught:
            self.service.publish("jan", "robot-arm", START, END, report["fingerprint"])
        self.assertEqual(caught.exception.code, "quality_block")

    def test_unresolved_event_blocks_release(self):
        report = self.full_scenario()
        self.ingest(20, "lot.receive", 31, {"lot_id": "bad", "part": "x",
                                               "supplier": "s", "quantity": 0})
        newer = self.service.report("robot-arm", START, END)
        self.assertEqual(newer["quality"]["issue_count"], 1)
        self.assertEqual(newer["quality"]["issues"][0]["code"], "unresolved_event")
        self.assertNotEqual(report["fingerprint"], newer["fingerprint"])

    def test_stale_preview_rejected_on_new_event(self):
        first = self.full_scenario()
        self.ingest(20, "lot.receive", 31, {"lot_id": "M2", "part": "motor",
                                               "supplier": "motors", "quantity": 2})
        with self.assertRaises(DomainError) as caught:
            self.service.publish("jan", "robot-arm", START, END, first["fingerprint"])
        self.assertEqual(caught.exception.code, "stale_preview")

    def test_release_idempotency_and_conflict(self):
        report = self.full_scenario()
        first = self.service.publish("jan", "robot-arm", START, END, report["fingerprint"])
        second = self.service.publish("jan", "robot-arm", START, END, report["fingerprint"])
        self.assertFalse(first["repeated"])
        self.assertTrue(second["repeated"])
        with self.assertRaises(DomainError) as caught:
            self.service.publish("jan", "robot-arm", START, END, "different")
        self.assertEqual(caught.exception.code, "release_conflict")

    def test_frozen_report_remains_after_late_event(self):
        report = self.full_scenario()
        frozen = self.service.publish("jan", "robot-arm", START, END, report["fingerprint"])
        self.ingest(20, "lot.receive", 31, {"lot_id": "M2", "part": "motor",
                                               "supplier": "motors", "quantity": 2})
        same = self.service.get_release("jan")
        self.assertEqual(frozen["fingerprint"], same["fingerprint"])
        self.assertEqual(frozen["source_head"], same["source_head"])
        self.assertTrue(self.service.shelf.freshness("jan")["current_differs"])

    def test_weekly_and_cohort_views_use_real_events(self):
        self.full_scenario()
        weekly = self.service.weekly("robot-arm", START, END)
        self.assertEqual(weekly["weeks"][0]["tested_units"], 4)
        cohorts = self.service.cohorts("robot-arm", START, END)
        self.assertEqual(cohorts["cohorts"][0]["revision"], 1)
        self.assertEqual(cohorts["cohorts"][0]["pass_rate"], "0.75")

    def test_bottleneck_view_separates_queue_and_active_time(self):
        self.full_scenario()
        view = self.service.bottlenecks("robot-arm", START, END)
        active = {item["stage"]: item for item in view["active"]}
        queue = {item["before_stage"]: item for item in view["queues"]}
        self.assertEqual(active["machining"]["active_seconds"], 300)
        self.assertEqual(queue["machining"]["wait_seconds"], 60)
        self.assertEqual(queue["assembly"]["wait_seconds"], 120)

    def test_supplier_exposure_is_nonadditive_attribution(self):
        self.full_scenario()
        exposure = self.service.supplier_exposure("robot-arm", START, END)
        self.assertEqual(len(exposure["suppliers"]), 2)
        self.assertEqual({item["associated_passed_units"] for item in exposure["suppliers"]}, {3})
        self.assertIn("not additive", exposure["note"])

    def test_compare_reports_delta(self):
        self.full_scenario()
        result = self.service.compare("robot-arm", START, END, "2026-02-01T00:00:00Z",
                                      "2026-03-01T00:00:00Z")
        self.assertEqual(result["delta"]["job_count"], -1)
        self.assertEqual(result["second"]["summary"]["job_count"], 0)

    def test_invalid_window_fails_before_query(self):
        with self.assertRaises(DomainError) as caught:
            self.service.report("robot-arm", END, START)
        self.assertEqual(caught.exception.code, "invalid_window")

    def test_published_report_audit_remains_valid(self):
        report = self.full_scenario()
        self.service.publish("jan", "robot-arm", START, END, report["fingerprint"])
        audit = self.service.audit("robot-arm")
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["finding_count"], 0)
        self.assertEqual(audit["checked_event_head"], 12)
