from collections import defaultdict
from datetime import datetime

from .analytics import CycleAnalytics, window
from .contracts import DomainError, duration_seconds, required_text, timestamp


class ThroughputView:
    def __init__(self, store):
        self.store = store
        self.analytics = CycleAnalytics(store)

    def weekly(self, product, start, end):
        report = self.analytics.report(product, start, end)
        weeks = defaultdict(lambda: {"opened_jobs": 0, "opened_units": 0,
                                     "finished_jobs": 0, "tested_units": 0,
                                     "passed_units": 0, "failed_units": 0,
                                     "rework_jobs": 0, "job_ids": []})
        for job in report["jobs"]:
            opened_week = datetime.fromisoformat(job["opened_at"]).strftime("%G-W%V")
            opened = weeks[opened_week]
            opened["opened_jobs"] += 1
            opened["opened_units"] += job["quantity"]
            opened["rework_jobs"] += bool(job["parent_job"])
            opened["job_ids"].append(job["job_id"])
            if job["completed_at"]:
                completed_week = datetime.fromisoformat(job["completed_at"]).strftime("%G-W%V")
                weeks[completed_week]["finished_jobs"] += 1
            for test in job["tests"]:
                tested_week = datetime.fromisoformat(test["tested_at"]).strftime("%G-W%V")
                group = weeks[tested_week]
                group["tested_units"] += test["passed"] + test["failed"]
                group["passed_units"] += test["passed"]
                group["failed_units"] += test["failed"]
        result = []
        for week, metrics in sorted(weeks.items()):
            result.append({"week": week, **metrics, "job_ids": sorted(metrics["job_ids"])})
        return {"product": report["product"], "window_start": report["window_start"],
                "window_end": report["window_end"], "weeks": result,
                "quality": report["quality"], "source_head": report["source_head"]}

    def bottlenecks(self, product, start, end):
        report = self.analytics.report(product, start, end)
        by_stage = defaultdict(lambda: {"active_seconds": 0, "completed_count": 0,
                                       "open_count": 0, "job_ids": []})
        gaps = defaultdict(lambda: {"wait_seconds": 0, "count": 0, "job_ids": []})
        for job in report["jobs"]:
            stages = job["stages"]
            earlier = job["opened_at"]
            for stage in stages:
                name = stage["stage"]
                group = by_stage[name]
                group["job_ids"].append(job["job_id"])
                if stage["duration_seconds"] is not None:
                    group["active_seconds"] += stage["duration_seconds"]
                    group["completed_count"] += 1
                else:
                    group["open_count"] += 1
                seconds = duration_seconds(earlier, stage["started_at"])
                wait = gaps[name]
                wait["wait_seconds"] += seconds
                wait["count"] += 1
                wait["job_ids"].append(job["job_id"])
                earlier = stage["completed_at"] or stage["started_at"]
        active = [{"stage": stage, **metrics, "job_ids": sorted(set(metrics["job_ids"]))}
                  for stage, metrics in sorted(by_stage.items())]
        queue = [{"before_stage": stage, **metrics, "job_ids": sorted(set(metrics["job_ids"]))}
                 for stage, metrics in sorted(gaps.items())]
        return {"product": report["product"], "active": active, "queues": queue,
                "mean_cycle_seconds": report["summary"]["mean_cycle_seconds"],
                "mean_wait_seconds": report["summary"]["mean_wait_seconds"],
                "quality": report["quality"]}

    def cohorts(self, product, start, end):
        report = self.analytics.report(product, start, end)
        groups = defaultdict(lambda: {"job_ids": [], "units": 0, "tested": 0,
                                      "passed": 0, "failed": 0, "mean_cycle_inputs": []})
        for job in report["jobs"]:
            key = (job["revision"], bool(job["parent_job"]))
            group = groups[key]
            group["job_ids"].append(job["job_id"])
            group["units"] += job["quantity"]
            if job["cycle_seconds"] is not None:
                group["mean_cycle_inputs"].append(job["cycle_seconds"])
            for test in job["tests"]:
                group["tested"] += test["passed"] + test["failed"]
                group["passed"] += test["passed"]
                group["failed"] += test["failed"]
        cohorts = []
        for (revision, rework), group in sorted(groups.items()):
            samples = group.pop("mean_cycle_inputs")
            tested = group["tested"]
            cohorts.append({"revision": revision, "rework": rework, **group,
                            "job_ids": sorted(group["job_ids"]),
                            "pass_rate": str(group["passed"] / tested) if tested else None,
                            "mean_cycle_seconds": sum(samples) // len(samples) if samples else None})
        return {"product": report["product"], "window_start": report["window_start"],
                "window_end": report["window_end"], "cohorts": cohorts,
                "quality": report["quality"]}
