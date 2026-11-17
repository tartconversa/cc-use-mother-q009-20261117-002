"""Comparable cohort analysis for pilot ramp-up yield decisions.

The engine never trusts an aggregate pass rate.  It rebuilds comparable cohorts
from the same accepted facts used by the frozen reports:

* each job becomes a classified unit with a configuration signature
  (design revision, rework flag, supplier profile and lot profile);
* cohorts that cannot be matched across both windows, whose lot composition
  changed, whose sample is too small or whose test coverage is too thin are
  excluded from the controlled comparison with an explicit reason;
* yield uncertainty is expressed with Wilson intervals per cohort and a
  two-proportion difference interval for the matched pool;
* the naive window-to-window rate difference is decomposed into a within-cohort
  quality effect, a configuration-mix effect and a volume effect (symmetric
  Kitagawa decomposition).

Every output is deterministic JSON built from integers and fixed-point
decimal strings so it can be fingerprinted and reproduced from a frozen
release report.
"""

from collections import defaultdict

from .analytics import window
from .contracts import DomainError, fingerprint, required_text, stable_json

Z_95 = 1.959963984540054
RATE_PLACES = 6

# Job statuses that never carry a test result and must not enter a rate.
UNFINISHED_STATUSES = ("open", "in_progress", "awaiting_test")


def fixed(number) -> str:
    """Round a float to a stable fixed-point decimal string."""
    return f"{number:.{RATE_PLACES}f}"


def wilson(passed: int, tested: int, z: float = Z_95):
    """Wilson score interval for a binomial proportion."""
    if tested <= 0:
        return None
    n = float(tested)
    p = passed / n
    width = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    low = max(0.0, (center - width / denominator))
    high = min(1.0, (center + width / denominator))
    return {"rate": fixed(p), "lower": fixed(low), "upper": fixed(high),
            "passed": passed, "tested": tested}


def difference_interval(passed_a, tested_a, passed_b, tested_b, z=Z_95):
    """Independent two-proportion difference interval: rate_b - rate_a."""
    if tested_a <= 0 or tested_b <= 0:
        return None
    p_a = passed_a / tested_a
    p_b = passed_b / tested_b
    variance = p_a * (1 - p_a) / tested_a + p_b * (1 - p_b) / tested_b
    margin = z * variance ** 0.5
    difference = p_b - p_a
    return {"difference": fixed(difference),
            "lower": fixed(difference - margin), "upper": fixed(difference + margin),
            "standard_error": fixed(variance ** 0.5)}


class CohortComparison:
    """Builds reproducible cohort comparisons from CycleAnalytics reports."""

    def __init__(self, store, *, min_tested_units=1, min_coverage=0.8,
                 require_same_lots=True):
        self.store = store
        if type(min_tested_units) is not int or min_tested_units < 1:
            raise DomainError("invalid_field", "min_tested_units must be a positive integer")
        if not 0 < min_coverage <= 1:
            raise DomainError("invalid_field", "min_coverage must lie within (0,1]")
        self.min_tested_units = min_tested_units
        self.min_coverage = min_coverage
        self.require_same_lots = bool(require_same_lots)

    # ------------------------------------------------------------------ public

    def compare(self, product, first_start, first_end, second_start, second_end):
        product = required_text(product, "product")
        first_start, first_end = window(first_start, first_end)
        second_start, second_end = window(second_start, second_end)
        from .analytics import CycleAnalytics
        analytics = CycleAnalytics(self.store)
        first = analytics.report(product, first_start, first_end)
        second = analytics.report(product, second_start, second_end)
        return self.analyze(first, second)

    def analyze(self, first_report, second_report):
        """Pure analysis over two report payloads (live or thawed releases)."""
        first = self._window_view(first_report, "first")
        second = self._window_view(second_report, "second")
        strata = self._build_strata(first, second)
        matched = [item for item in strata if item["eligible_for_comparison"]]
        matched_keys = {self._tuple_key(item) for item in matched}
        naive = self._naive_comparison(first, second)
        matched_rate = self._matched_comparison(first, second, matched_keys)
        decomposition = self._decompose(first, second, strata, matched_keys)
        interpretation = self._interpret(naive, matched_rate, strata)
        public_strata = []
        for stratum in strata:
            item = dict(stratum)
            item.pop("tuple_key")
            public_strata.append(item)
        result = {
            "product": first_report["product"],
            "windows": {"first": {"start": first_report["window_start"], "end": first_report["window_end"]},
                        "second": {"start": second_report["window_start"], "end": second_report["window_end"]}},
            "source_head": {"first": first_report["source_head"], "second": second_report["source_head"]},
            "config": {"min_tested_units": self.min_tested_units,
                       "min_coverage": fixed(self.min_coverage),
                       "require_same_lots": self.require_same_lots},
            "coverage": {"first": first["coverage"], "second": second["coverage"]},
            "data_gaps": {"first": first["gaps"], "second": second["gaps"]},
            "job_inventory": {"first": first["jobs"], "second": second["jobs"]},
            "strata": public_strata,
            "matched_strata": sorted((item["key"] for item in public_strata
                                      if item["eligible_for_comparison"]),
                                     key=lambda item: stable_json(item)),
            "naive_comparison": naive,
            "matched_comparison": matched_rate,
            "decomposition": decomposition,
            "interpretation": interpretation,
        }
        result["fingerprint"] = fingerprint(result)
        return result

    def drift(self, release_id):
        """Compare a frozen release cohort view against the current facts."""
        from .snapshot import ReportShelf
        shelf = ReportShelf(self.store)
        frozen_envelope = shelf.get(release_id)
        frozen_report = frozen_envelope["report"]
        from .analytics import CycleAnalytics
        current_report = CycleAnalytics(self.store).report(
            frozen_report["product"], frozen_report["window_start"], frozen_report["window_end"])
        frozen_analysis = self.analyze(frozen_report, self._empty_twin(frozen_report))
        current_analysis = self.analyze(current_report, self._empty_twin(current_report))
        changes = self._job_changes(frozen_report, current_report)
        verdict_changed = (frozen_analysis["interpretation"]["verdict"]
                           != current_analysis["interpretation"]["verdict"])
        return {
            "release_id": release_id,
            "frozen_source_head": frozen_envelope["source_head"],
            "current_source_head": current_report["source_head"],
            "frozen_fingerprint": frozen_envelope["fingerprint"],
            "current_report_fingerprint": current_report["fingerprint"],
            "frozen_verdict": frozen_analysis["interpretation"]["verdict"],
            "current_verdict": current_analysis["interpretation"]["verdict"],
            "verdict_changed": verdict_changed,
            "frozen_matched_rate": frozen_analysis["matched_comparison"],
            "current_matched_rate": current_analysis["matched_comparison"],
            "frozen_naive_rate": frozen_analysis["naive_comparison"],
            "current_naive_rate": current_analysis["naive_comparison"],
            "job_changes": changes,
            "frozen_analysis_fingerprint": frozen_analysis["fingerprint"],
            "current_analysis_fingerprint": current_analysis["fingerprint"],
        }

    # ------------------------------------------------------------- window view

    def _window_view(self, report, label):
        jobs = []
        gaps = []
        tested_units = eligible_units = untested_units = scrapped_units = 0
        unfinished_jobs = untested_jobs = scrapped_jobs = 0
        for job in report["jobs"]:
            tested = sum(test["passed"] + test["failed"] for test in job["tests"])
            passed = sum(test["passed"] for test in job["tests"])
            supplier_signature, lot_signature, supplier_profile = self._supplier_profile(job)
            classified = {
                "job_id": job["job_id"], "revision": job["revision"],
                "rework": bool(job["parent_job"]), "parent_job": job["parent_job"],
                "status": job["status"], "quantity": job["quantity"],
                "tested_units": tested, "passed_units": passed,
                "supplier_signature": supplier_signature,
                "lot_signature": lot_signature,
                "supplier_profile": supplier_profile,
                "test_event_ids": sorted(test["source_event_id"] for test in job["tests"]),
                "test_ids": sorted(test["test_id"] for test in job["tests"]),
            }
            jobs.append(classified)
            if job["status"] in UNFINISHED_STATUSES:
                unfinished_jobs += 1
                untested_units += job["quantity"] - tested
                reason = "awaiting_test_without_record" if job["status"] == "awaiting_test" else "unfinished_job"
                gaps.append({"code": reason, "subject": job["job_id"],
                             "units_without_test": job["quantity"] - tested})
                continue
            if job["status"] == "scrapped":
                scrapped_jobs += 1
                scrapped_units += job["quantity"]
                gaps.append({"code": "scrapped_job_excluded", "subject": job["job_id"],
                             "units": job["quantity"], "reason": job.get("failure_reason")})
                continue
            # Terminal testable jobs (completed / failed): rates only over tested units.
            eligible_units += job["quantity"]
            tested_units += tested
            if tested == 0:
                untested_jobs += 1
                untested_units += job["quantity"]
                gaps.append({"code": "terminal_job_without_test", "subject": job["job_id"],
                             "status": job["status"], "units_without_test": job["quantity"]})
            elif tested < job["quantity"]:
                untested_units += job["quantity"] - tested
                gaps.append({"code": "partial_test_coverage", "subject": job["job_id"],
                             "units_without_test": job["quantity"] - tested})
        for issue in report["quality"]["issues"]:
            if issue["code"] == "unresolved_event":
                gaps.append({"code": "unresolved_event", "subject": issue["subject"],
                             "reason": issue.get("reason")})
        gaps.sort(key=lambda item: (item["code"], item["subject"]))
        coverage = self._coverage(tested_units, eligible_units)
        view = {
            "label": label,
            "jobs": sorted(jobs, key=lambda item: item["job_id"]),
            "gaps": gaps,
            "coverage": {
                "tested_units": tested_units,
                "testable_units": eligible_units,
                "rate": coverage["rate"],
                "sufficient": coverage["sufficient"],
                "threshold": coverage.get("threshold"),
                "reason": coverage.get("reason"),
                "untested_units": untested_units,
                "scrapped_units": scrapped_units,
                "job_count": len(jobs),
                "untested_job_count": untested_jobs,
                "unfinished_job_count": unfinished_jobs,
                "scrapped_job_count": scrapped_jobs,
            },
        }
        return view

    def _coverage(self, tested, testable):
        if testable == 0:
            return {"rate": None, "sufficient": False, "reason": "no_testable_jobs"}
        rate = tested / testable
        return {"rate": fixed(rate), "sufficient": rate >= self.min_coverage,
                "threshold": fixed(self.min_coverage)}

    def _supplier_profile(self, job):
        by_part = defaultdict(dict)
        lot_parts = {}
        for lot in job["lots"]:
            # Quantities accumulate for the profile display, but the grouping
            # signature only names suppliers: cohort size must not split a cohort.
            by_part[lot["part"]][lot["supplier"]] = (
                by_part[lot["part"]].get(lot["supplier"], 0) + lot["quantity"])
            lot_parts[lot["lot_id"]] = lot["part"]
        profile = {part: dict(sorted(mix.items())) for part, mix in sorted(by_part.items())}
        supplier_signature = tuple(sorted((part, tuple(sorted(mix)))
                                          for part, mix in by_part.items()))
        lot_signature = tuple(sorted((part, lot_id) for lot_id, part in lot_parts.items()))
        return supplier_signature, lot_signature, profile

    # ------------------------------------------------------------------ strata

    def _build_strata(self, first, second):
        grouped = defaultdict(lambda: {"windows": {}})
        for view in (first, second):
            buckets = defaultdict(list)
            for job in view["jobs"]:
                key = (job["revision"], job["rework"], job["supplier_signature"])
                buckets[key].append(job)
            for key, members in buckets.items():
                grouped[key]["windows"][view["label"]] = members
        result = []
        for key in sorted(grouped, key=self._sort_key):
            result.append(self._stratum(key, grouped[key]["windows"], first["label"], second["label"]))
        return result

    @staticmethod
    def _sort_key(key):
        revision, rework, supplier_signature = key
        return (revision, int(rework), tuple(str(term) for term in supplier_signature))

    def _stratum(self, key, windows, first_label, second_label):
        revision, rework, supplier_signature = key
        exclusion_reasons = []
        lot_signatures = set()
        window_blocks = {}
        for label in (first_label, second_label):
            members = windows.get(label, [])
            units = sum(job["quantity"] for job in members)
            tested = sum(job["tested_units"] for job in members)
            passed = sum(job["passed_units"] for job in members)
            for job in members:
                lot_signatures.add(job["lot_signature"])
            block = {
                "job_ids": sorted(job["job_id"] for job in members),
                "job_count": len(members), "units": units,
                "tested_units": tested, "passed_units": passed,
                "failed_units": tested - passed,
                "test_coverage": fixed(tested / units) if units else None,
                "interval": wilson(passed, tested),
            }
            window_blocks[label] = block
            if members and tested < self.min_tested_units:
                exclusion_reasons.append(
                    {"code": "insufficient_sample", "window": label, "tested_units": tested})
            if members and units and tested / units < self.min_coverage:
                exclusion_reasons.append(
                    {"code": "below_coverage_threshold", "window": label,
                     "coverage": fixed(tested / units), "threshold": fixed(self.min_coverage)})
        present = {label for label, members in windows.items() if members}
        if present != {first_label, second_label}:
            exclusion_reasons.append({
                "code": "one_sided_stratum",
                "window": next(iter(present)),
                "detail": "cohort exists in only one window; excluded from controlled comparison"})
        if self.require_same_lots and len(lot_signatures) > 1:
            ordered = sorted(lot_signatures,
                             key=lambda value: self._stable_text(
                                 self._render_lot_signature(value)))
            exclusion_reasons.append({
                "code": "lot_composition_changed",
                "lot_signatures": [self._render_lot_signature(signature)
                                   for signature in ordered]})
        eligible = not exclusion_reasons
        first_block = window_blocks[first_label]
        second_block = window_blocks[second_label]
        delta = difference_interval(first_block["passed_units"], first_block["tested_units"],
                                    second_block["passed_units"], second_block["tested_units"])
        return {
            "tuple_key": key,
            "key": self._render_key(key),
            "revision": revision, "rework": rework,
            "supplier_profile": self._render_supplier_signature(supplier_signature),
            "first": first_block, "second": second_block,
            "matched_delta": delta,
            "eligible_for_comparison": eligible,
            "exclusion_reasons": exclusion_reasons,
        }

    @staticmethod
    def _tuple_key(stratum):
        return (stratum["revision"], stratum["rework"],
                tuple((part, tuple(suppliers))
                      for part, suppliers in sorted(stratum["supplier_profile"].items())))

    @staticmethod
    def _render_key(key):
        revision, rework, signature = key
        return {"revision": revision, "rework": rework,
                "suppliers": CohortComparison._render_supplier_signature(signature)}

    @staticmethod
    def _render_supplier_signature(signature):
        # Signature terms name suppliers only; quantities live in the profile.
        return {part: sorted(suppliers) for part, suppliers in signature}

    @staticmethod
    def _render_lot_signature(signature):
        rendered = defaultdict(list)
        for part, lot_id in signature:
            rendered[part].append(lot_id)
        return {part: sorted(lots) for part, lots in sorted(rendered.items())}

    @staticmethod
    def _stable_text(value):
        return stable_json(value)

    # ------------------------------------------------------------- comparisons

    def _naive_comparison(self, first, second):
        a = self._pool(first)
        b = self._pool(second)
        return self._rate_block("naive", a, b)

    def _matched_comparison(self, first, second, matched_keys):
        def pool(view):
            passed = tested = units = 0
            for job in view["jobs"]:
                key = (job["revision"], job["rework"], job["supplier_signature"])
                if key in matched_keys:
                    passed += job["passed_units"]
                    tested += job["tested_units"]
                    units += job["quantity"]
            return {"passed": passed, "tested": tested, "units": units}
        a, b = pool(first), pool(second)
        block = self._rate_block("matched", a, b)
        block["matched_unit_share"] = {
            "first": fixed(a["units"] / first["coverage"]["testable_units"])
            if first["coverage"]["testable_units"] else None,
            "second": fixed(b["units"] / second["coverage"]["testable_units"])
            if second["coverage"]["testable_units"] else None}
        return block

    @staticmethod
    def _pool(view):
        passed = sum(job["passed_units"] for job in view["jobs"])
        tested = sum(job["tested_units"] for job in view["jobs"])
        units = sum(job["quantity"] for job in view["jobs"]
                    if job["status"] not in UNFINISHED_STATUSES and job["status"] != "scrapped")
        return {"passed": passed, "tested": tested, "units": units}

    def _rate_block(self, name, a, b):
        return {
            "name": name,
            "first": {"units": a["units"], "tested_units": a["tested"],
                      "passed_units": a["passed"], "interval": wilson(a["passed"], a["tested"])},
            "second": {"units": b["units"], "tested_units": b["tested"],
                       "passed_units": b["passed"], "interval": wilson(b["passed"], b["tested"])},
            "delta_interval": difference_interval(a["passed"], a["tested"], b["passed"], b["tested"]),
        }

    # ------------------------------------------------------------- decomposition

    def _decompose(self, first, second, strata, matched_keys):
        """Symmetric Kitagawa split of the naive pooled rate difference."""
        rates = {}
        weights = {}
        for label, view in (("first", first), ("second", second)):
            total_units = sum(
                job["quantity"] for job in view["jobs"]
                if job["status"] not in UNFINISHED_STATUSES and job["status"] != "scrapped")
            weights[label] = {"__total__": total_units}
            for stratum in strata:
                key = stratum["tuple_key"]
                block = stratum[label]
                rates[(label, key)] = (
                    block["passed_units"] / block["tested_units"] if block["tested_units"] else None)
                weights[label][key] = block["units"]
        within = mix = volume = 0.0
        contributions = []
        for stratum in strata:
            key = stratum["tuple_key"]
            w1 = weights["first"].get(key, 0)
            w2 = weights["second"].get(key, 0)
            total1 = weights["first"]["__total__"] or 1
            total2 = weights["second"]["__total__"] or 1
            share1 = w1 / total1
            share2 = w2 / total2
            r1 = rates[("first", key)]
            r2 = rates[("second", key)]
            if r1 is not None and r2 is not None:
                within_part = (share1 + share2) / 2 * (r2 - r1)
                mix_part = (r1 + r2) / 2 * (share2 - share1)
                volume_part = 0.0
            elif r2 is not None:
                within_part = mix_part = 0.0
                volume_part = share2 * r2
            elif r1 is not None:
                within_part = mix_part = 0.0
                volume_part = -share1 * r1
            else:
                continue
            within += within_part
            mix += mix_part
            volume += volume_part
            contributions.append({"stratum": stratum["key"], "within_effect": fixed(within_part),
                                  "mix_effect": fixed(mix_part), "volume_effect": fixed(volume_part)})
        naive_delta = self._naive_delta(first, second)
        return {
            "naive_rate_difference": naive_delta,
            "within_cohort_quality_effect": fixed(within),
            "configuration_mix_effect": fixed(mix),
            "volume_effect": fixed(volume),
            "components_sum": fixed(within + mix + volume),
            "stratum_contributions": sorted(contributions,
                                            key=lambda item: stable_json(item["stratum"])),
            "note": ("Effects split the naive second-minus-first rate difference. "
                     "Within = quality change holding mix fixed; mix = configuration "
                     "share change holding quality fixed; volume = cohorts present in "
                     "only one window. Components sum to the naive difference.")
        }

    @staticmethod
    def _naive_delta(first, second):
        a = CohortComparison._pool(first)
        b = CohortComparison._pool(second)
        if not a["tested"] or not b["tested"]:
            return None
        return fixed(b["passed"] / b["tested"] - a["passed"] / a["tested"])

    # ------------------------------------------------------------- interpretation

    def _interpret(self, naive, matched, all_strata):
        notes = []
        matched_count = sum(1 for item in all_strata if item["eligible_for_comparison"])
        excluded = [item for item in all_strata if not item["eligible_for_comparison"]]
        if not matched_count:
            verdict = "inconclusive_no_comparable_cohort"
            notes.append("No cohort is present in both windows under the configured comparability rules.")
        else:
            interval = matched["delta_interval"]
            lower = float(interval["lower"])
            upper = float(interval["upper"])
            if lower > 0:
                verdict = "improvement_within_comparable_cohorts"
            elif upper < 0:
                verdict = "regression_within_comparable_cohorts"
            else:
                verdict = "no_detectable_change"
                notes.append("Matched-pool 95% interval contains zero; sample size may be insufficient.")
        naive_interval = naive["delta_interval"]
        if naive_interval and matched["delta_interval"]:
            naive_sign = self._sign(float(naive_interval["difference"]))
            matched_sign = self._sign(float(matched["delta_interval"]["difference"]))
            if naive_sign != 0 and matched_sign != 0 and naive_sign != matched_sign:
                notes.append(
                    "Naive aggregate and matched-cohort directions disagree (Simpson-style reversal); "
                    "configuration mix, not within-cohort quality, drives the headline rate.")
        for item in excluded:
            notes.append(f"Excluded stratum rev {item['revision']} rework={item['rework']}: "
                         + ", ".join(reason["code"] for reason in item["exclusion_reasons"]))
        return {"verdict": verdict, "matched_stratum_count": matched_count,
                "excluded_stratum_count": len(excluded), "notes": notes}

    @staticmethod
    def _sign(value):
        return (value > 0) - (value < 0)

    # ------------------------------------------------------------------- drift

    def _job_changes(self, frozen_report, current_report):
        frozen_jobs = {job["job_id"]: job for job in frozen_report["jobs"]}
        current_jobs = {job["job_id"]: job for job in current_report["jobs"]}
        added, removed, changed = [], [], []
        for job_id in sorted(set(current_jobs) - set(frozen_jobs)):
            added.append(self._job_change_detail(current_jobs[job_id], "added"))
        for job_id in sorted(set(frozen_jobs) - set(current_jobs)):
            removed.append(self._job_change_detail(frozen_jobs[job_id], "removed"))
        for job_id in sorted(set(frozen_jobs) & set(current_jobs)):
            before, after = frozen_jobs[job_id], current_jobs[job_id]
            fields = ("status", "quantity", "revision", "parent_job")
            diffs = {field: {"frozen": before.get(field), "current": after.get(field)}
                     for field in fields if before.get(field) != after.get(field)}
            before_tests = sum(t["passed"] + t["failed"] for t in before["tests"])
            after_tests = sum(t["passed"] + t["failed"] for t in after["tests"])
            if before_tests != after_tests:
                diffs["tested_units"] = {"frozen": before_tests, "current": after_tests}
            before_passed = sum(t["passed"] for t in before["tests"])
            after_passed = sum(t["passed"] for t in after["tests"])
            if before_passed != after_passed:
                diffs["passed_units"] = {"frozen": before_passed, "current": after_passed}
            if diffs:
                changed.append({"job_id": job_id, "change": "changed", "fields": diffs})
        return {"added": added, "removed": removed, "changed": changed,
                "added_count": len(added), "removed_count": len(removed),
                "changed_count": len(changed)}

    @staticmethod
    def _job_change_detail(job, kind):
        return {"job_id": job["job_id"], "change": kind, "revision": job["revision"],
                "status": job["status"], "quantity": job["quantity"],
                "tested_units": sum(t["passed"] + t["failed"] for t in job["tests"])}

    @staticmethod
    def _empty_twin(report):
        return {"product": report["product"],
                "window_start": report["window_start"], "window_end": report["window_end"],
                "source_head": report["source_head"], "jobs": [],
                "quality": {"publishable": True, "issue_count": 0, "issues": []}}
