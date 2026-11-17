import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


class MissingDependency(DomainError):
    def __init__(self, kind: str, identifier: str):
        super().__init__("missing_dependency", f"{kind} {identifier} does not exist", 409)
        self.kind = kind
        self.identifier = identifier


def required_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError("invalid_field", f"{name} must be nonempty text")
    result = value.strip()
    if len(result) > 160:
        raise DomainError("invalid_field", f"{name} is too long")
    return result


def nonnegative_int(value, name: str, *, positive=False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise DomainError("invalid_field", f"{name} must be {'positive' if positive else 'nonnegative'} integer")
    return value


def decimal_value(value, name: str, *, minimum=0) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DomainError("invalid_field", f"{name} must be decimal") from None
    if not number.is_finite() or number < Decimal(str(minimum)):
        raise DomainError("invalid_field", f"{name} outside valid range")
    return str(number.normalize())


def timestamp(value, name="occurred_at") -> str:
    if not isinstance(value, str):
        raise DomainError("invalid_time", f"{name} requires ISO-8601 timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_time", f"{name} requires ISO-8601 timezone") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DomainError("invalid_time", f"{name} requires ISO-8601 timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def duration_seconds(start: str, end: str) -> int:
    seconds = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    if seconds < 0:
        raise DomainError("time_order", "end precedes start")
    return seconds


def keys(payload: dict, required: set[str], allowed: set[str] | None = None) -> None:
    if not isinstance(payload, dict):
        raise DomainError("invalid_payload", "payload must be object")
    missing = required - payload.keys()
    if missing:
        raise DomainError("missing_field", f"missing fields: {', '.join(sorted(missing))}")
    extra = payload.keys() - (allowed or required)
    if extra:
        raise DomainError("unknown_field", f"unknown fields: {', '.join(sorted(extra))}")


def row_dict(row):
    return dict(row) if row is not None else None
