import argparse
import json
import sys

from .contracts import DomainError
from .service import CycleService


def parser():
    root = argparse.ArgumentParser(prog="cycleflow", description="Offline prototype iteration analytics")
    root.add_argument("--db", default="cycleflow.sqlite3")
    sub = root.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("file", help="JSON object event")
    batch = sub.add_parser("batch")
    batch.add_argument("batch_id")
    batch.add_argument("file", help="JSON array of events")
    batch.add_argument("--stop-after", type=int)
    retry = sub.add_parser("retry")
    retry.add_argument("event_id", type=int)
    sub.add_parser("retry-waiting")
    sub.add_parser("problems")
    audit = sub.add_parser("audit")
    audit.add_argument("--product")
    capacity = sub.add_parser("capacity")
    capacity.add_argument("design_id")
    capacity.add_argument("quantity", type=int)
    capacity.add_argument("at")
    timeline = sub.add_parser("timeline")
    timeline.add_argument("job_id")
    report = sub.add_parser("report")
    report.add_argument("product")
    report.add_argument("start")
    report.add_argument("end")
    publish = sub.add_parser("publish")
    publish.add_argument("release_id")
    publish.add_argument("product")
    publish.add_argument("start")
    publish.add_argument("end")
    publish.add_argument("fingerprint")
    release = sub.add_parser("release")
    release.add_argument("release_id")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    service = CycleService(args.db)
    try:
        if args.command == "ingest":
            with open(args.file, encoding="utf-8") as handle:
                result = service.ingest(json.load(handle))
        elif args.command == "batch":
            with open(args.file, encoding="utf-8") as handle:
                result = service.ingest_batch(args.batch_id, json.load(handle), args.stop_after)
        elif args.command == "retry":
            result = service.retry(args.event_id)
        elif args.command == "retry-waiting":
            result = service.retry_waiting()
        elif args.command == "problems":
            result = service.problems()
        elif args.command == "audit":
            result = service.audit(args.product)
        elif args.command == "capacity":
            result = service.capacity(args.design_id, args.quantity, args.at)
        elif args.command == "timeline":
            result = service.job_timeline(args.job_id)
        elif args.command == "report":
            result = service.report(args.product, args.start, args.end)
        elif args.command == "publish":
            result = service.publish(args.release_id, args.product, args.start, args.end, args.fingerprint)
        elif args.command == "release":
            result = service.get_release(args.release_id)
        else:
            raise AssertionError(args.command)
    except (DomainError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "input_error"), "message": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
