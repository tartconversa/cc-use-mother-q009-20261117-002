import json
import sqlite3

from cycleflow.contracts import DomainError
from cycleflow.demo import END, START, event
from tests.common import ServiceCase


class EventFlowTests(ServiceCase):
    def test_same_event_replays_without_extra_projection(self):
        original = event(1, "design.release", 0, {"design_id": "R1", "product": "robot-arm",
                                                    "revision": 1, "parts": {"motor": 2}})
        first = self.service.ingest(original)
        again = self.service.ingest(original)
        self.assertEqual(first["id"], again["id"])
        self.assertFalse(first["repeated"])
        self.assertTrue(again["repeated"])
        self.assertEqual(again["attempts"], 1)
        self.assertEqual(self.service.health()["event_head"], 1)
        self.assertEqual(self.service.get_design("R1")["parts"], {"motor": 2})

    def test_content_conflict_preserves_first_event(self):
        item = event(1, "lot.receive", 0, {"lot_id": "L1", "part": "motor",
                                                "supplier": "a", "quantity": 3})
        self.service.ingest(item)
        item["payload"]["quantity"] = 4
        with self.assertRaises(DomainError) as caught:
            self.service.ingest(item)
        self.assertEqual(caught.exception.code, "identity_conflict")
        self.assertEqual(self.service.get_lot("L1")["quantity"], 3)
        self.assertEqual(self.service.health()["event_head"], 1)

    def test_missing_dependency_can_retry_after_design(self):
        late = self.ingest(10, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 1})
        self.assertEqual(late["state"], "waiting")
        self.assertEqual(late["error_code"], "missing_dependency")
        self.seed_design()
        self.seed_lots()
        retried = self.service.retry(late["id"])
        self.assertEqual(retried["state"], "accepted")
        self.assertEqual(retried["attempts"], 2)
        self.assertEqual(self.service.get_job("J1")["status"], "open")

    def test_waiting_remains_waiting_when_partial_dependency_arrives(self):
        late = self.ingest(10, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 1})
        self.seed_design()
        retried = self.service.retry(late["id"])
        self.assertEqual(retried["state"], "waiting")
        self.assertEqual(retried["error_code"], "shortage")
        self.assertEqual(self.service.health()["unresolved_events"], 1)

    def test_rejected_event_cannot_retry(self):
        rejected = self.ingest(1, "lot.receive", 0, {"lot_id": "L1", "part": "x",
                                                      "supplier": "s", "quantity": 0})
        self.assertEqual(rejected["state"], "rejected")
        with self.assertRaises(DomainError) as caught:
            self.service.retry(rejected["id"])
        self.assertEqual(caught.exception.code, "not_retryable")
        self.assertEqual(self.service.health()["unresolved_events"], 1)

    def test_unknown_event_kind_rejected_before_insert(self):
        item = event(1, "unknown.operation", 0, {})
        with self.assertRaises(DomainError) as caught:
            self.service.ingest(item)
        self.assertEqual(caught.exception.code, "unknown_kind")
        self.assertEqual(self.service.health()["event_head"], 0)

    def test_naive_timestamp_rejected_before_insert(self):
        item = event(1, "lot.receive", 0, {})
        item["occurred_at"] = "2026-01-01T10:00:00"
        with self.assertRaises(DomainError) as caught:
            self.service.ingest(item)
        self.assertEqual(caught.exception.code, "invalid_time")
        self.assertEqual(self.service.health()["event_head"], 0)

    def test_missing_and_unknown_fields_are_distinct(self):
        item = event(1, "lot.receive", 0, {"lot_id": "L1"})
        item.pop("source")
        with self.assertRaises(DomainError) as missing:
            self.service.ingest(item)
        self.assertEqual(missing.exception.code, "missing_field")
        item["source"] = "test"
        item["unexpected"] = 5
        with self.assertRaises(DomainError) as extra:
            self.service.ingest(item)
        self.assertEqual(extra.exception.code, "unknown_field")

    def test_projection_failure_does_not_create_partial_rows(self):
        self.seed_design()
        result = self.ingest(20, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 4})
        self.assertEqual(result["error_code"], "shortage")
        with self.service.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_retry_waiting_is_bounded_and_ordered(self):
        first = self.ingest(10, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 1})
        second = self.ingest(11, "job.open", 5, {"job_id": "J2", "design_id": "R1", "quantity": 1})
        self.seed_design()
        self.seed_lots()
        result = self.service.retry_waiting(limit=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], first["id"])
        self.assertEqual(self.service.get_event(second["id"])["state"], "waiting")
        self.assertEqual(self.service.retry_waiting()[0]["state"], "accepted")

    def test_event_lineage_includes_job_and_lots(self):
        report = self.full_scenario()
        trace = self.service.get_lineage(12)
        self.assertEqual(trace["kind"], "test.record")
        self.assertEqual(trace["trace"]["plan"]["plan_id"], "P1")
        self.assertEqual(trace["trace"]["job_lineage"][0]["job_id"], "J1")
        self.assertEqual({r["lot_id"] for r in trace["trace"]["reservations"]}, {"M1", "S1"})
        self.assertIn(12, report["source_event_ids"])
