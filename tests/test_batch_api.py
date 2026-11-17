from fastapi.testclient import TestClient

from cycleflow.api import create_app
from cycleflow.contracts import DomainError
from cycleflow.demo import END, START, scenario
from tests.common import ServiceCase


class BatchApiTests(ServiceCase):
    def test_batch_resume_and_manifest_identity(self):
        events = scenario()
        first = self.service.ingest_batch("b1", events, stop_after=3)
        self.assertEqual(first["next_line"], 3)
        self.assertFalse(first["completed"])
        second = self.service.ingest_batch("b1", events, stop_after=2)
        self.assertEqual(second["next_line"], 5)
        final = self.service.ingest_batch("b1", events)
        self.assertTrue(final["completed"])
        self.assertEqual(final["next_line"], 12)
        self.assertEqual(self.service.batches.status("b1")["state"], "completed")

    def test_batch_conflict_does_not_advance_cursor(self):
        events = scenario()
        self.service.ingest_batch("b1", events, stop_after=2)
        changed = scenario()
        changed[1]["payload"]["quantity"] = 99
        with self.assertRaises(DomainError) as caught:
            self.service.ingest_batch("b1", changed)
        self.assertEqual(caught.exception.code, "batch_conflict")
        self.assertEqual(self.service.batches.status("b1")["next_line"], 2)

    def test_batch_records_invalid_line_and_continues(self):
        events = scenario()
        events.insert(1, {"bad": "event"})
        result = self.service.ingest_batch("b1", events)
        self.assertTrue(result["completed"])
        self.assertEqual(result["outcomes"][1]["outcome"]["state"], "invalid")
        self.assertEqual(self.service.get_job("J1")["status"], "completed")

    def test_http_ingest_report_publish_and_audit(self):
        client = TestClient(create_app(str(self.path)))
        for item in scenario():
            response = client.post("/events", json=item)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["state"], "accepted")
        report = client.get("/reports/robot-arm", params={"window_start": START, "window_end": END})
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["summary"]["passed_units"], 3)
        published = client.post("/releases", json={"release_id": "jan", "product": "robot-arm",
                                                   "window_start": START, "window_end": END,
                                                   "expected_fingerprint": report.json()["fingerprint"]})
        self.assertEqual(published.status_code, 200, published.text)
        self.assertTrue(client.get("/quality/audit").json()["ok"])
        self.assertEqual(client.get("/releases/jan").json()["report"]["summary"]["tested_units"], 4)

    def test_http_domain_error_is_structured(self):
        client = TestClient(create_app(str(self.path)))
        response = client.post("/events", json={"source": "x", "external_id": "1",
                                                      "kind": "unknown", "occurred_at": START,
                                                      "payload": {}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "unknown_kind")
        self.assertEqual(client.get("/health").json()["event_head"], 0)

    def test_http_batch_resume_and_capacity(self):
        client = TestClient(create_app(str(self.path)))
        events = scenario()
        first = client.post("/batches/b1", json={"events": events, "stop_after": 5})
        self.assertEqual(first.json()["next_line"], 5)
        self.assertEqual(client.get("/batches/b1").json()["state"], "running")
        next_run = client.post("/batches/b1", json={"events": events})
        self.assertTrue(next_run.json()["completed"])
        capacity = client.get("/designs/R1/capacity", params={"quantity": 1, "at": "2026-01-12T09:31:00Z"})
        self.assertEqual(capacity.status_code, 200)
        self.assertEqual(capacity.json()["max_units"], 0)

    def test_http_event_trace_contains_all_business_links(self):
        client = TestClient(create_app(str(self.path)))
        for item in scenario():
            client.post("/events", json=item)
        trace = client.get("/events/12/lineage")
        self.assertEqual(trace.status_code, 200)
        self.assertEqual(trace.json()["trace"]["plan"]["revision"], 1)
        self.assertEqual(len(trace.json()["trace"]["reservations"]), 2)

    def test_http_quality_block_and_stale_preview(self):
        client = TestClient(create_app(str(self.path)))
        events = scenario()
        for item in events[:5]:
            self.assertEqual(client.post("/events", json=item).status_code, 200)
        preview = client.get("/reports/robot-arm", params={"window_start": START, "window_end": END}).json()
        blocked = client.post("/releases", json={"release_id": "jan", "product": "robot-arm",
                                                 "window_start": START, "window_end": END,
                                                 "expected_fingerprint": preview["fingerprint"]})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"], "quality_block")
        for item in events[5:]:
            self.assertEqual(client.post("/events", json=item).status_code, 200)
        stale = client.post("/releases", json={"release_id": "jan", "product": "robot-arm",
                                               "window_start": START, "window_end": END,
                                               "expected_fingerprint": preview["fingerprint"]})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"], "stale_preview")
        self.assertTrue(client.get("/quality/audit").json()["ok"])
