import json

from .contracts import DomainError, MissingDependency, keys, nonnegative_int, required_text, stable_json, timestamp


class DesignRules:
    def __init__(self, db):
        self.db = db

    def release(self, payload, occurred_at):
        keys(payload, {"design_id", "product", "revision", "parts"},
             {"design_id", "product", "revision", "parts", "parent_id", "specification"})
        design_id = required_text(payload["design_id"], "design_id")
        product = required_text(payload["product"], "product")
        revision = nonnegative_int(payload["revision"], "revision", positive=True)
        parent_id = payload.get("parent_id")
        if parent_id is not None:
            parent_id = required_text(parent_id, "parent_id")
        parts = payload["parts"]
        if not isinstance(parts, dict) or not parts:
            raise DomainError("invalid_parts", "parts must be a nonempty object")
        normalized = {}
        for part, qty in parts.items():
            normalized[required_text(part, "part")] = nonnegative_int(qty, "quantity", positive=True)
        if len(normalized) != len(parts):
            raise DomainError("duplicate_part", "normalized part names collide")
        specification = payload.get("specification", {})
        if not isinstance(specification, dict):
            raise DomainError("invalid_specification", "specification must be an object")
        if parent_id:
            parent = self.db.execute("SELECT * FROM designs WHERE design_id=?", (parent_id,)).fetchone()
            if parent is None:
                raise MissingDependency("parent design", parent_id)
            if parent["product"] != product or parent["revision"] >= revision:
                raise DomainError("invalid_parent", "parent must be an earlier revision of same product")
            if parent["released_at"] > occurred_at:
                raise DomainError("time_order", "design cannot precede parent release")
        elif revision != 1:
            raise DomainError("missing_parent", "revision above one requires parent_id")
        else:
            existing = self.db.execute("SELECT design_id FROM designs WHERE product=?", (product,)).fetchone()
            if existing:
                raise DomainError("duplicate_root", "product already has root design")
        latest = self.db.execute("SELECT MAX(revision) FROM designs WHERE product=?", (product,)).fetchone()[0]
        if latest is not None and revision != latest + 1:
            raise DomainError("revision_gap", "revisions must be consecutive")
        self.db.execute("INSERT INTO designs(design_id,product,revision,parent_id,released_at,specification) VALUES(?,?,?,?,?,?)",
                        (design_id, product, revision, parent_id, occurred_at, stable_json(specification)))
        self.db.executemany("INSERT INTO requirements(design_id,part,quantity) VALUES(?,?,?)",
                            [(design_id, part, quantity) for part, quantity in sorted(normalized.items())])
        return {"design_id": design_id, "product": product, "revision": revision}

    def retire(self, payload, occurred_at):
        keys(payload, {"design_id"})
        design_id = required_text(payload["design_id"], "design_id")
        design = self.get(design_id)
        if design["state"] == "retired":
            raise DomainError("already_retired", "design already retired")
        if occurred_at < design["released_at"]:
            raise DomainError("time_order", "retirement precedes release")
        open_jobs = self.db.execute("SELECT COUNT(*) FROM jobs WHERE design_id=? AND status NOT IN ('completed','scrapped')",
                                    (design_id,)).fetchone()[0]
        if open_jobs:
            raise DomainError("active_jobs", "cannot retire design with open jobs")
        self.db.execute("UPDATE designs SET state='retired' WHERE design_id=?", (design_id,))
        return {"design_id": design_id, "state": "retired"}

    def get(self, design_id):
        row = self.db.execute("SELECT * FROM designs WHERE design_id=?", (design_id,)).fetchone()
        if row is None:
            raise MissingDependency("design", design_id)
        result = dict(row)
        result["specification"] = json.loads(result["specification"])
        result["parts"] = {r["part"]: r["quantity"] for r in self.db.execute(
            "SELECT part,quantity FROM requirements WHERE design_id=? ORDER BY part", (design_id,))}
        return result

    def lineage(self, design_id):
        lineage = []
        seen = set()
        current = design_id
        while current:
            if current in seen:
                raise DomainError("cyclic_lineage", "design ancestry is cyclic")
            seen.add(current)
            design = self.get(current)
            lineage.append({"design_id": current, "revision": design["revision"]})
            current = design["parent_id"]
        return lineage

    def list_product(self, product):
        return [self.get(row["design_id"]) for row in self.db.execute(
            "SELECT design_id FROM designs WHERE product=? ORDER BY revision", (product,))]

    def dispatch(self, kind, payload, occurred_at):
        if kind == "design.release":
            return self.release(payload, occurred_at)
        if kind == "design.retire":
            return self.retire(payload, occurred_at)
        raise DomainError("unknown_kind", f"unsupported design event {kind}")
