import json
from collections import defaultdict

from .contracts import DomainError, MissingDependency, fingerprint, required_text
from .design import DesignRules
from .execution import ExecutionRules
from .inventory import InventoryRules
from .measure import MeasurementRules


class IntegrityAudit:
    def __init__(self, store):
        self.store = store

    def run(self, product=None):
        if product is not None:
            product = required_text(product, "product")
        with self.store.read() as db:
            findings = []
            findings.extend(self._event_checks(db))
            findings.extend(self._design_checks(db, product))
            findings.extend(self._lot_checks(db))
            findings.extend(self._job_checks(db, product))
            findings.extend(self._test_checks(db, product))
            findings.extend(self._release_checks(db, product))
            findings.sort(key=lambda item: (item["code"], item["subject"]))
            return {"product": product, "checked_event_head": db.execute(
                "SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0],
                "ok": not findings, "finding_count": len(findings), "findings": findings}

    def _event_checks(self, db):
        findings = []
        for row in db.execute("SELECT * FROM events ORDER BY id"):
            identity = {"source": row["source"], "external_id": row["external_id"],
                        "kind": row["kind"], "occurred_at": row["occurred_at"],
                        "payload": json.loads(row["payload"])}
            if fingerprint(identity) != row["fingerprint"]:
                findings.append(self._issue("event_fingerprint", str(row["id"])))
            if row["state"] == "accepted" and row["error_code"]:
                findings.append(self._issue("accepted_with_error", str(row["id"])))
            if row["state"] != "accepted" and not row["error_code"]:
                findings.append(self._issue("unexplained_event", str(row["id"])))
            if row["attempts"] < 1:
                findings.append(self._issue("unattempted_event", str(row["id"])))
        return findings

    def _design_checks(self, db, product):
        findings = []
        sql = "SELECT * FROM designs" + (" WHERE product=?" if product else "") + " ORDER BY product,revision"
        previous = {}
        for design in db.execute(sql, (product,) if product else ()):
            name = design["product"]
            parent = previous.get(name)
            if design["revision"] == 1:
                if design["parent_id"] is not None:
                    findings.append(self._issue("root_parent", design["design_id"]))
            elif parent is None or design["parent_id"] != parent["design_id"]:
                findings.append(self._issue("design_chain", design["design_id"]))
            if parent and design["revision"] != parent["revision"] + 1:
                findings.append(self._issue("revision_gap", design["design_id"]))
            if parent and design["released_at"] < parent["released_at"]:
                findings.append(self._issue("release_order", design["design_id"]))
            count = db.execute("SELECT COUNT(*) FROM requirements WHERE design_id=?",
                               (design["design_id"],)).fetchone()[0]
            if count == 0:
                findings.append(self._issue("missing_bom", design["design_id"]))
            previous[name] = design
        return findings

    def _lot_checks(self, db):
        findings = []
        for lot in db.execute("SELECT * FROM lots ORDER BY lot_id"):
            reserved = db.execute("SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE lot_id=?",
                                  (lot["lot_id"],)).fetchone()[0]
            if reserved > lot["quantity"]:
                findings.append(self._issue("lot_overallocated", lot["lot_id"]))
            if lot["quarantined"] and reserved:
                findings.append(self._issue("quarantined_in_use", lot["lot_id"]))
            if lot["quantity"] <= 0:
                findings.append(self._issue("invalid_lot_quantity", lot["lot_id"]))
        return findings

    def _job_checks(self, db, product):
        findings = []
        sql = ("SELECT j.*,d.product FROM jobs j JOIN designs d ON j.design_id=d.design_id" +
               (" WHERE d.product=?" if product else "") + " ORDER BY j.job_id")
        for job in db.execute(sql, (product,) if product else ()):
            job_id = job["job_id"]
            stages = list(db.execute("SELECT * FROM job_steps WHERE job_id=? ORDER BY started_at,stage", (job_id,)))
            if len(stages) > 3:
                findings.append(self._issue("too_many_stages", job_id))
            previous_end = job["opened_at"]
            expected = ("machining", "assembly", "integration")
            for index, stage in enumerate(stages):
                if index >= len(expected) or stage["stage"] != expected[index]:
                    findings.append(self._issue("stage_sequence", job_id))
                if stage["started_at"] < previous_end:
                    findings.append(self._issue("stage_overlap", job_id))
                if stage["completed_at"] and stage["completed_at"] < stage["started_at"]:
                    findings.append(self._issue("negative_stage", job_id))
                previous_end = stage["completed_at"] or stage["started_at"]
            terminal = job["status"] in ("completed", "failed", "scrapped")
            if terminal and not job["completed_at"]:
                findings.append(self._issue("terminal_without_time", job_id))
            if job["status"] == "awaiting_test" and (len(stages) != 3 or not stages[-1]["completed_at"]):
                findings.append(self._issue("premature_test_state", job_id))
            if job["parent_job"]:
                parent = db.execute("SELECT status FROM jobs WHERE job_id=?", (job["parent_job"],)).fetchone()
                if parent is None or parent["status"] not in ("failed", "scrapped"):
                    findings.append(self._issue("invalid_rework_parent", job_id))
            if job["status"] != "scrapped":
                assigned = {row["part"]: row["quantity"] for row in db.execute(
                    "SELECT l.part,SUM(r.quantity) quantity FROM reservations r JOIN lots l ON l.lot_id=r.lot_id WHERE r.job_id=? GROUP BY l.part",
                    (job_id,))}
                required = {row["part"]: row["quantity"] * job["quantity"] for row in db.execute(
                    "SELECT part,quantity FROM requirements WHERE design_id=?", (job["design_id"],))}
                if assigned != required:
                    findings.append(self._issue("bom_allocation", job_id))
        return findings

    def _test_checks(self, db, product):
        findings = []
        sql = ("SELECT t.*,j.quantity,j.status,j.design_id,d.product,d.revision FROM tests t"
               " JOIN jobs j ON j.job_id=t.job_id JOIN designs d ON d.design_id=j.design_id" +
               (" WHERE d.product=?" if product else "") + " ORDER BY t.test_id")
        for test in db.execute(sql, (product,) if product else ()):
            subject = test["test_id"]
            if test["passed"] + test["failed"] != test["quantity"]:
                findings.append(self._issue("test_quantity", subject))
            plan = db.execute("SELECT * FROM plans WHERE plan_id=?", (test["plan_id"],)).fetchone()
            if plan is None or (plan["product"], plan["revision"]) != (test["product"], test["revision"]):
                findings.append(self._issue("test_plan_revision", subject))
            if test["status"] not in ("completed", "failed"):
                findings.append(self._issue("test_job_status", subject))
        return findings

    def _release_checks(self, db, product):
        findings = []
        sql = "SELECT * FROM releases" + (" WHERE product=?" if product else "")
        for release in db.execute(sql, (product,) if product else ()):
            report = json.loads(release["report"])
            digest = report.pop("fingerprint", None)
            if digest != fingerprint(report) or digest != release["fingerprint"]:
                findings.append(self._issue("release_fingerprint", release["release_id"]))
            if not report.get("quality", {}).get("publishable"):
                findings.append(self._issue("release_quality", release["release_id"]))
        return findings

    @staticmethod
    def _issue(code, subject):
        return {"code": code, "subject": subject}
