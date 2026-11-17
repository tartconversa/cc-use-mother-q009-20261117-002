from .analytics import CycleAnalytics
from .audit import IntegrityAudit
from .batch import BatchReceiver
from .contracts import DomainError
from .design import DesignRules
from .diagnostics import OperationsView
from .execution import ExecutionRules
from .inventory import InventoryRules
from .ledger import EventLedger
from .measure import MeasurementRules
from .snapshot import ReportShelf
from .storage import Store
from .throughput import ThroughputView


class CycleService:
    def __init__(self, path):
        self.store = Store(path)
        self.ledger = EventLedger(self.store)
        self.batches = BatchReceiver(self.store)
        self.analytics = CycleAnalytics(self.store)
        self.shelf = ReportShelf(self.store)
        self.audit_engine = IntegrityAudit(self.store)
        self.operations = OperationsView(self.store)
        self.throughput = ThroughputView(self.store)

    def ingest(self, event):
        return self.ledger.ingest(event)

    def ingest_batch(self, batch_id, events, stop_after=None):
        return self.batches.submit(batch_id, events, stop_after=stop_after)

    def retry(self, event_id):
        return self.ledger.retry(event_id)

    def retry_waiting(self, limit=100):
        return self.ledger.retry_waiting(limit)

    def report(self, product, start, end):
        return self.analytics.report(product, start, end)

    def compare(self, product, first_start, first_end, second_start, second_end):
        return self.analytics.compare(product, first_start, first_end, second_start, second_end)

    def publish(self, release_id, product, start, end, expected_fingerprint):
        return self.shelf.publish(release_id, product, start, end, expected_fingerprint)

    def get_release(self, release_id):
        return self.shelf.get(release_id)

    def get_design(self, design_id):
        with self.store.read() as db:
            return DesignRules(db).get(design_id)

    def get_lot(self, lot_id):
        with self.store.read() as db:
            return InventoryRules(db).get(lot_id)

    def get_job(self, job_id):
        with self.store.read() as db:
            return ExecutionRules(db).get(job_id)

    def get_test(self, test_id):
        with self.store.read() as db:
            return MeasurementRules(db).trace(test_id)

    def get_event(self, event_id):
        return self.ledger.get(event_id)

    def get_lineage(self, event_id):
        return self.ledger.lineage(event_id)

    def problems(self):
        return self.ledger.problems()

    def health(self):
        return {"status": "ok", "event_head": self.store.scalar("SELECT COALESCE(MAX(id),0) FROM events"),
                "unresolved_events": self.store.scalar("SELECT COUNT(*) FROM events WHERE state!='accepted'")}

    def audit(self, product=None):
        return self.audit_engine.run(product)

    def capacity(self, design_id, quantity, at):
        return self.operations.capacity(design_id, quantity, at)

    def job_timeline(self, job_id):
        return self.operations.job_timeline(job_id)

    def supplier_exposure(self, product, start, end):
        return self.operations.supplier_exposure(product, start, end)

    def weekly(self, product, start, end):
        return self.throughput.weekly(product, start, end)

    def bottlenecks(self, product, start, end):
        return self.throughput.bottlenecks(product, start, end)

    def cohorts(self, product, start, end):
        return self.throughput.cohorts(product, start, end)
