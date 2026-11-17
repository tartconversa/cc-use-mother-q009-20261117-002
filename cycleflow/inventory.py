from .contracts import DomainError, MissingDependency, keys, nonnegative_int, required_text


class InventoryRules:
    def __init__(self, db):
        self.db = db

    def receive(self, payload, occurred_at):
        keys(payload, {"lot_id", "part", "supplier", "quantity"})
        lot_id = required_text(payload["lot_id"], "lot_id")
        part = required_text(payload["part"], "part")
        supplier = required_text(payload["supplier"], "supplier")
        quantity = nonnegative_int(payload["quantity"], "quantity", positive=True)
        self.db.execute("INSERT INTO lots(lot_id,part,supplier,received_at,quantity) VALUES(?,?,?,?,?)",
                        (lot_id, part, supplier, occurred_at, quantity))
        return {"lot_id": lot_id, "available": quantity}

    def quarantine(self, payload, occurred_at):
        keys(payload, {"lot_id", "quarantined"})
        lot_id = required_text(payload["lot_id"], "lot_id")
        if type(payload["quarantined"]) is not bool:
            raise DomainError("invalid_field", "quarantined must be boolean")
        lot = self.get(lot_id)
        if occurred_at < lot["received_at"]:
            raise DomainError("time_order", "quarantine precedes receipt")
        if payload["quarantined"] and lot["reserved"]:
            raise DomainError("lot_in_use", "cannot quarantine reserved lot")
        self.db.execute("UPDATE lots SET quarantined=? WHERE lot_id=?", (int(payload["quarantined"]), lot_id))
        return {"lot_id": lot_id, "quarantined": payload["quarantined"]}

    def get(self, lot_id):
        row = self.db.execute("SELECT * FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise MissingDependency("lot", lot_id)
        result = dict(row)
        result["reserved"] = self.db.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE lot_id=?", (lot_id,)).fetchone()[0]
        result["available"] = result["quantity"] - result["reserved"]
        result["quarantined"] = bool(result["quarantined"])
        return result

    def choose(self, part, required, at):
        candidates = self.db.execute(
            "SELECT lot_id FROM lots WHERE part=? AND received_at<=? AND quarantined=0 ORDER BY received_at,lot_id",
            (part, at)).fetchall()
        allocations = []
        remaining = required
        for candidate in candidates:
            lot = self.get(candidate["lot_id"])
            take = min(lot["available"], remaining)
            if take:
                allocations.append((lot["lot_id"], take))
                remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise DomainError("shortage", f"insufficient available {part}: need {required}, missing {remaining}", 409)
        return allocations

    def reserve(self, job_id, design, quantity, occurred_at):
        allocations = []
        for part, per_unit in sorted(design["parts"].items()):
            allocations.extend(self.choose(part, per_unit * quantity, occurred_at))
        for lot_id, take in allocations:
            self.db.execute("INSERT INTO reservations(job_id,lot_id,quantity) VALUES(?,?,?)",
                            (job_id, lot_id, take))
        return [{"lot_id": lot_id, "quantity": take} for lot_id, take in allocations]

    def reservations(self, job_id):
        return [dict(row) for row in self.db.execute(
            "SELECT r.job_id,r.lot_id,l.part,l.supplier,r.quantity FROM reservations r JOIN lots l ON l.lot_id=r.lot_id WHERE r.job_id=? ORDER BY l.part,r.lot_id",
            (job_id,))]

    def release(self, job_id):
        released = self.db.execute("SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE job_id=?",
                                   (job_id,)).fetchone()[0]
        self.db.execute("DELETE FROM reservations WHERE job_id=?", (job_id,))
        return released

    def availability(self, part):
        result = []
        for row in self.db.execute("SELECT lot_id FROM lots WHERE part=? ORDER BY received_at,lot_id", (part,)):
            result.append(self.get(row["lot_id"]))
        return result

    def dispatch(self, kind, payload, occurred_at):
        if kind == "lot.receive":
            return self.receive(payload, occurred_at)
        if kind == "lot.quarantine":
            return self.quarantine(payload, occurred_at)
        raise DomainError("unknown_kind", f"unsupported inventory event {kind}")
