from cycleflow.contracts import DomainError
from cycleflow.demo import END, START
from tests.common import ServiceCase


class DesignInventoryTests(ServiceCase):
    def test_revision_requires_parent_and_consecutive_number(self):
        self.seed_design()
        missing = self.ingest(20, "design.release", 10, {"design_id": "R3", "product": "robot-arm",
                                                           "revision": 3, "parent_id": "R1",
                                                           "parts": {"motor": 2}})
        self.assertEqual(missing["error_code"], "revision_gap")
        valid = self.ingest(21, "design.release", 11, {"design_id": "R2", "product": "robot-arm",
                                                         "revision": 2, "parent_id": "R1",
                                                         "parts": {"motor": 1, "sensor": 2}})
        self.assertEqual(valid["state"], "accepted")
        self.assertEqual(self.service.get_design("R2")["parts"]["sensor"], 2)

    def test_parent_must_match_product(self):
        self.seed_design()
        outcome = self.ingest(20, "design.release", 10, {"design_id": "R2", "product": "other",
                                                           "revision": 2, "parent_id": "R1",
                                                           "parts": {"motor": 2}})
        self.assertEqual(outcome["error_code"], "invalid_parent")
        self.assertEqual(self.service.health()["unresolved_events"], 1)

    def test_design_retirement_blocked_with_open_jobs(self):
        self.seed_design()
        self.seed_lots()
        self.seed_job()
        blocked = self.ingest(20, "design.retire", 30, {"design_id": "R1"})
        self.assertEqual(blocked["error_code"], "active_jobs")
        self.assertEqual(self.service.get_design("R1")["state"], "released")

    def test_retired_design_cannot_open_new_job(self):
        self.seed_design()
        self.seed_lots()
        retired = self.ingest(20, "design.retire", 3, {"design_id": "R1"})
        self.assertEqual(retired["state"], "accepted")
        rejected = self.seed_job()
        self.assertEqual(rejected["error_code"], "retired_design")
        self.assertEqual(self.service.get_design("R1")["state"], "retired")

    def test_multi_lot_allocation_uses_receipt_order(self):
        self.seed_design()
        self.ingest(2, "lot.receive", 1, {"lot_id": "M1", "part": "motor",
                                              "supplier": "a", "quantity": 3})
        self.ingest(3, "lot.receive", 2, {"lot_id": "M2", "part": "motor",
                                              "supplier": "b", "quantity": 5})
        self.ingest(4, "lot.receive", 3, {"lot_id": "S1", "part": "sensor",
                                              "supplier": "c", "quantity": 4})
        opened = self.seed_job()
        self.assertEqual(opened["state"], "accepted")
        rows = self.service.get_job("J1")["reservations"]
        self.assertEqual([(r["lot_id"], r["quantity"]) for r in rows], [("M1", 3), ("M2", 5), ("S1", 4)])
        self.assertEqual(self.service.get_lot("M1")["available"], 0)
        self.assertEqual(self.service.get_lot("M2")["available"], 0)

    def test_quarantine_rejects_reserved_lot(self):
        self.seed_design()
        self.seed_lots()
        self.seed_job()
        outcome = self.ingest(20, "lot.quarantine", 10, {"lot_id": "M1", "quarantined": True})
        self.assertEqual(outcome["error_code"], "lot_in_use")
        self.assertFalse(self.service.get_lot("M1")["quarantined"])

    def test_quarantine_prevents_future_reservation(self):
        self.seed_design()
        self.seed_lots()
        accepted = self.ingest(20, "lot.quarantine", 3, {"lot_id": "M1", "quarantined": True})
        self.assertEqual(accepted["state"], "accepted")
        denied = self.seed_job()
        self.assertEqual(denied["error_code"], "shortage")
        self.assertTrue(self.service.get_lot("M1")["quarantined"])

    def test_capacity_explains_shortage_by_part(self):
        self.seed_design()
        self.seed_lots()
        view = self.service.capacity("R1", 7, "2026-01-12T09:04:00Z")
        self.assertFalse(view["feasible"])
        self.assertEqual(view["max_units"], 6)
        by_part = {item["part"]: item for item in view["requirements"]}
        self.assertEqual(by_part["motor"]["shortage"], 2)
        self.assertEqual(by_part["sensor"]["shortage"], 1)

    def test_capacity_respects_receipt_time(self):
        self.seed_design()
        self.seed_lots()
        before = self.service.capacity("R1", 1, "2026-01-12T09:00:30Z")
        after = self.service.capacity("R1", 1, "2026-01-12T09:02:00Z")
        self.assertEqual(before["max_units"], 0)
        self.assertEqual(after["max_units"], 6)
        self.assertFalse(before["feasible"])
        self.assertTrue(after["feasible"])

    def test_duplicate_normalized_part_is_rejected(self):
        outcome = self.ingest(1, "design.release", 0, {"design_id": "R1", "product": "p",
                                                         "revision": 1, "parts": {"motor": 1, " motor ": 2}})
        self.assertEqual(outcome["error_code"], "duplicate_part")
        self.assertEqual(self.service.health()["unresolved_events"], 1)
