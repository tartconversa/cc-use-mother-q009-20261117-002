from collections import defaultdict
from datetime import datetime

from .analytics import CycleAnalytics, window
from .contracts import DomainError, MissingDependency, duration_seconds, required_text
from .execution import ExecutionRules
from .inventory import InventoryRules


class OperationsView:
    def __init__(self, store):
        self.store = store
        self.analytics = CycleAnalytics(store)

    def capacity(self, design_id, quantity, at):
        design_id = required_text(design_id, "design_id")
        if type(quantity) is not int or quantity < 1:
            raise DomainError("invalid_quantity", "quantity must be positive integer")
        from .contracts import timestamp
        at = timestamp(at, "at")
        with self.store.read() as db:
            design = ExecutionRules(db).designs.get(design_id)
            inventory = InventoryRules(db)
            requirements = []
            possible = []
            for part, per_unit in sorted(design["parts"].items()):
                lots = [lot for lot in inventory.availability(part)
                        if lot["received_at"] <= at and not lot["quarantined"]]
                available = sum(lot["available"] for lot in lots)
                maximum = available // per_unit
                possible.append(maximum)
                requirements.append({"part": part, "per_unit": per_unit,
                                     "requested": per_unit * quantity, "available": available,
                                     "lot_ids": [lot["lot_id"] for lot in lots],
                                     "shortage": max(0, per_unit * quantity - available)})
            maximum = min(possible) if possible else 0
            return {"design_id": design_id, "quantity": quantity, "at": at,
                    "feasible": quantity <= maximum and design["state"] == "released"
                    and at >= design["released_at"],
                    "max_units": maximum, "design_state": design["state"],
                    "requirements": requirements}

    def job_timeline(self, job_id):
        job_id = required_text(job_id, "job_id")
        with self.store.read() as db:
            job = ExecutionRules(db).get(job_id)
            milestones = [{"at": job["opened_at"], "kind": "opened", "stage": None}]
            for stage in job["stages"]:
                milestones.append({"at": stage["started_at"], "kind": "stage_start",
                                   "stage": stage["stage"], "source_event_id": stage["event_id"]})
                if stage["completed_at"]:
                    milestones.append({"at": stage["completed_at"], "kind": "stage_finish",
                                       "stage": stage["stage"]})
            for test in db.execute("SELECT test_id,tested_at,passed,failed,event_id FROM tests WHERE job_id=?", (job_id,)):
                milestones.append({"at": test["tested_at"], "kind": "tested", "stage": None,
                                   "test_id": test["test_id"], "passed": test["passed"],
                                   "failed": test["failed"], "source_event_id": test["event_id"]})
            if job["status"] == "scrapped":
                milestones.append({"at": job["completed_at"], "kind": "scrapped", "stage": None})
            milestones.sort(key=lambda item: (item["at"], item["kind"]))
            gaps = []
            for earlier, later in zip(milestones, milestones[1:]):
                elapsed = duration_seconds(earlier["at"], later["at"])
                if elapsed:
                    gaps.append({"from": earlier["kind"], "to": later["kind"],
                                 "seconds": elapsed, "start": earlier["at"], "end": later["at"]})
            return {"job_id": job_id, "status": job["status"], "milestones": milestones,
                    "gaps": gaps, "reservations": job["reservations"],
                    "lineage": ExecutionRules(db).lineage(job_id)}

    def supplier_exposure(self, product, start, end):
        report = self.analytics.report(product, start, end)
        exposure = defaultdict(lambda: {"jobs": set(), "units": 0, "passed": 0,
                                        "failed": 0, "lots": set(), "revisions": set()})
        for job in report["jobs"]:
            suppliers = set()
            for lot in job["lots"]:
                group = exposure[lot["supplier"]]
                group["jobs"].add(job["job_id"])
                group["lots"].add(lot["lot_id"])
                group["units"] += lot["quantity"]
                group["revisions"].add(job["revision"])
                suppliers.add(lot["supplier"])
            for supplier in suppliers:
                for test in job["tests"]:
                    exposure[supplier]["passed"] += test["passed"]
                    exposure[supplier]["failed"] += test["failed"]
        result = []
        for name, group in sorted(exposure.items()):
            result.append({"supplier": name, "jobs": sorted(group["jobs"]),
                           "reserved_part_units": group["units"], "lots": sorted(group["lots"]),
                           "revisions": sorted(group["revisions"]),
                           "associated_passed_units": group["passed"],
                           "associated_failed_units": group["failed"]})
        return {"product": report["product"], "window_start": report["window_start"],
                "window_end": report["window_end"], "suppliers": result,
                "note": "Associated test units are attribution links, not additive across suppliers"}

    def event_trace(self, source, external_id):
        source = required_text(source, "source")
        external_id = required_text(external_id, "external_id")
        with self.store.read() as db:
            row = db.execute("SELECT id FROM events WHERE source=? AND external_id=?",
                             (source, external_id)).fetchone()
            if row is None:
                raise MissingDependency("event identity", f"{source}:{external_id}")
            event_id = row["id"]
        from .ledger import EventLedger
        return EventLedger(self.store).lineage(event_id)
