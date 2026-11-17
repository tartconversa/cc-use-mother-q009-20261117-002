from decimal import Decimal

from .contracts import DomainError, MissingDependency, decimal_value, keys, nonnegative_int, required_text
from .execution import ExecutionRules


class MeasurementRules:
    def __init__(self, db):
        self.db = db
        self.jobs = ExecutionRules(db)

    def create_plan(self, payload, occurred_at):
        keys(payload, {"plan_id", "product", "revision", "minimum_pass_rate"})
        plan_id = required_text(payload["plan_id"], "plan_id")
        product = required_text(payload["product"], "product")
        revision = nonnegative_int(payload["revision"], "revision", positive=True)
        minimum = decimal_value(payload["minimum_pass_rate"], "minimum_pass_rate")
        if Decimal(minimum) > 1:
            raise DomainError("invalid_threshold", "minimum_pass_rate cannot exceed one")
        design = self.db.execute("SELECT design_id,released_at FROM designs WHERE product=? AND revision=?",
                                 (product, revision)).fetchone()
        if design is None:
            raise MissingDependency("design revision", f"{product}:{revision}")
        if occurred_at < design["released_at"]:
            raise DomainError("time_order", "plan precedes design release")
        self.db.execute("INSERT INTO plans(plan_id,product,revision,minimum_pass_rate,effective_at) VALUES(?,?,?,?,?)",
                        (plan_id, product, revision, minimum, occurred_at))
        return {"plan_id": plan_id, "product": product, "revision": revision, "threshold": minimum}

    def record(self, payload, occurred_at, event_id):
        keys(payload, {"test_id", "job_id", "plan_id", "passed", "failed"},
             {"test_id", "job_id", "plan_id", "passed", "failed", "note"})
        test_id = required_text(payload["test_id"], "test_id")
        job_id = required_text(payload["job_id"], "job_id")
        plan_id = required_text(payload["plan_id"], "plan_id")
        passed = nonnegative_int(payload["passed"], "passed")
        failed = nonnegative_int(payload["failed"], "failed")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise DomainError("invalid_note", "note must be short text")
        job = self.jobs.get(job_id)
        if job["status"] != "awaiting_test":
            raise DomainError("job_not_testable", "job must complete integration before testing", 409)
        if passed + failed != job["quantity"]:
            raise DomainError("sample_mismatch", "tested units must equal job quantity")
        plan = self.get_plan(plan_id)
        if plan["product"] != job["design"]["product"] or plan["revision"] != job["design"]["revision"]:
            raise DomainError("plan_mismatch", "plan must match exact design revision")
        if occurred_at < max(plan["effective_at"], job["completed_at"]):
            raise DomainError("time_order", "test precedes plan or integration finish")
        rate = Decimal(passed) / Decimal(job["quantity"])
        accepted = rate >= Decimal(plan["minimum_pass_rate"])
        self.db.execute("INSERT INTO tests(test_id,job_id,plan_id,tested_at,passed,failed,note,event_id) VALUES(?,?,?,?,?,?,?,?)",
                        (test_id, job_id, plan_id, occurred_at, passed, failed, note, event_id))
        self.db.execute("UPDATE jobs SET status=?,failure_reason=? WHERE job_id=?",
                        ("completed" if accepted else "failed", None if accepted else "quality_threshold", job_id))
        return {"test_id": test_id, "job_id": job_id, "accepted": accepted,
                "pass_rate": str(rate), "threshold": plan["minimum_pass_rate"]}

    def get_plan(self, plan_id):
        row = self.db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise MissingDependency("plan", plan_id)
        return dict(row)

    def tests_for_job(self, job_id):
        self.jobs.get(job_id)
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM tests WHERE job_id=? ORDER BY tested_at,test_id", (job_id,))]

    def trace(self, test_id):
        row = self.db.execute("SELECT * FROM tests WHERE test_id=?", (test_id,)).fetchone()
        if row is None:
            raise MissingDependency("test", test_id)
        test = dict(row)
        test["plan"] = self.get_plan(test["plan_id"])
        test["job_lineage"] = self.jobs.lineage(test["job_id"])
        test["reservations"] = self.jobs.inventory.reservations(test["job_id"])
        return test

    def dispatch(self, kind, payload, occurred_at, event_id):
        if kind == "plan.create":
            return self.create_plan(payload, occurred_at)
        if kind == "test.record":
            return self.record(payload, occurred_at, event_id)
        raise DomainError("unknown_kind", f"unsupported measurement event {kind}")
