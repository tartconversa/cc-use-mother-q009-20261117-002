from cycleflow.contracts import fingerprint
from tests.common import ServiceCase


W1_START, W1_END = "2026-01-05T00:00:00Z", "2026-01-12T00:00:00Z"
W2_START, W2_END = "2026-01-12T00:00:00Z", "2026-01-19T00:00:00Z"


class CohortBuilder:
    """Programmatic event builder with unique, monotonic event numbering."""

    def __init__(self, service, source=None):
        self.service = service
        self.number = 0
        # A distinct source per builder keeps event identities unique across tests.
        self.source = source or f"cohort-{id(self)}"

    def add(self, kind, at, payload, source=None):
        self.number += 1
        event = {"source": source or self.source, "external_id": f"E{self.number}",
                 "kind": kind, "occurred_at": at, "payload": payload}
        return self.service.ingest(event)

    def design(self, design_id, revision, parent=None, parts=None):
        payload = {"design_id": design_id, "product": "robot-arm",
                   "revision": revision, "parts": parts or {"motor": 1, "sensor": 1}}
        if parent:
            payload["parent_id"] = parent
        return self.add("design.release", f"2026-01-01T00:0{revision}:00Z", payload)

    def lot(self, lot_id, part, supplier, quantity, day=1, hour=8):
        return self.add("lot.receive", f"2026-01-{day:02d}T{hour:02d}:00:00Z",
                        {"lot_id": lot_id, "part": part, "supplier": supplier,
                         "quantity": quantity})

    def plan(self, plan_id, revision, threshold="0.50"):
        return self.add("plan.create", f"2026-01-02T0{revision}:00:00Z",
                        {"plan_id": plan_id, "product": "robot-arm",
                         "revision": revision, "minimum_pass_rate": threshold})

    def complete_job(self, job_id, design_id, quantity, day, passed, failed,
                     plan_id, parent=None):
        at = f"2026-01-{day:02d}T09:00:00Z"
        payload = {"job_id": job_id, "design_id": design_id, "quantity": quantity}
        if parent:
            payload["parent_job"] = parent
        opened = self.add("job.open", at, payload)
        if opened["state"] != "accepted":
            return opened
        for hour, stage in ((1, "machining"), (2, "assembly"), (3, "integration")):
            self.add("job.stage_start", f"2026-01-{day:02d}T1{hour}:00:00Z",
                     {"job_id": job_id, "stage": stage})
            self.add("job.stage_finish", f"2026-01-{day:02d}T1{hour}:30:00Z",
                     {"job_id": job_id, "stage": stage})
        return self.add("test.record", f"2026-01-{day:02d}T15:00:00Z",
                        {"test_id": f"T-{job_id}", "job_id": job_id,
                         "plan_id": plan_id, "passed": passed, "failed": failed})

    def open_unfinished(self, job_id, design_id, quantity, day):
        return self.add("job.open", f"2026-01-{day:02d}T09:00:00Z",
                        {"job_id": job_id, "design_id": design_id, "quantity": quantity})


def build_simpsons_world(service):
    """Two windows where the naive rate moves with mix while each cohort improves.

    Two configurations recur in both windows.  The premium configuration is
    design revision 2 with an alpha gearbox; the standard configuration is
    revision 1 with a beta gearbox.  Premium's production share is small in
    window 1 and dominant in window 2, so the headline rate rises with the
    configuration mix even though every matched cohort improves on its own.
    """
    builder = CohortBuilder(service)
    builder.design("R1", 1, parts={"motor": 1, "sensor": 1})
    builder.design("R2", 2, parent="R1", parts={"servo": 1, "sensor": 1})
    # The revisions use distinct part numbers, so FIFO can never mix their
    # supplier cohorts: standard motors come from beta, premium servos from alpha.
    builder.lot("M-STD", "motor", "beta-motors", 200)
    # The premium lot holds exactly the 50 baseline premium units; the dedicated
    # lot-change test tops supply up with a second lot, forcing a new signature.
    builder.lot("V-PREM", "servo", "alpha-motors", 50)
    builder.lot("S-A", "sensor", "alpha-sensors", 400)
    builder.plan("P1", 1)
    builder.plan("P2", 2)
    # Window 1: 40 standard (rev1) at 60%, 10 premium (rev2) at 80% -> 64%
    builder.complete_job("J-STANDARD-1", "R1", 40, 6, 24, 16, "P1")
    builder.complete_job("J-PREMIUM-1", "R2", 10, 7, 8, 2, "P2")
    # Window 2: 10 standard at 70%, 40 premium at 90% -> 86%
    builder.complete_job("J-STANDARD-2", "R1", 10, 13, 7, 3, "P1")
    builder.complete_job("J-PREMIUM-2", "R2", 40, 14, 36, 4, "P2")
    return builder


class CohortEngineTests(ServiceCase):
    def setUp(self):
        super().setUp()
        build_simpsons_world(self.service)

    def compare(self, **kwargs):
        return self.service.cohort_compare(
            "robot-arm", W1_START, W1_END, W2_START, W2_END, **kwargs)

    def test_matched_cohorts_improve_while_naive_rate_moves_with_mix(self):
        result = self.compare()
        # Both strata recur in both windows with identical lot signatures.
        self.assertEqual(result["interpretation"]["matched_stratum_count"], 2)
        self.assertEqual(result["interpretation"]["excluded_stratum_count"], 0)
        # Every cohort improves point-wise, but the 10-unit cohorts are too small
        # for their individual 95% intervals to exclude zero — that uncertainty
        # must remain visible rather than being hidden by a significance claim.
        for stratum in result["strata"]:
            self.assertTrue(stratum["eligible_for_comparison"])
            self.assertGreater(float(stratum["matched_delta"]["difference"]), 0)
        # The pooled matched comparison (50+50 units) does exclude zero.
        interval = result["matched_comparison"]["delta_interval"]
        self.assertGreater(float(interval["lower"]), 0)
        # Naive rates: 0.64 -> 0.86, driven by the premium configuration gaining share.
        self.assertEqual(result["naive_comparison"]["first"]["interval"]["rate"], "0.640000")
        self.assertEqual(result["naive_comparison"]["second"]["interval"]["rate"], "0.860000")

    def test_kitagawa_decomposition_reconstructs_naive_difference(self):
        result = self.compare()
        decomposition = result["decomposition"]
        reconstructed = (float(decomposition["within_cohort_quality_effect"])
                         + float(decomposition["configuration_mix_effect"])
                         + float(decomposition["volume_effect"]))
        self.assertAlmostEqual(reconstructed,
                               float(decomposition["naive_rate_difference"]), places=5)
        # No one-sided cohorts, so the volume effect must be exactly zero.
        self.assertEqual(decomposition["volume_effect"], "0.000000")
        # Mix effect dominates the within effect in this constructed scenario.
        self.assertGreater(abs(float(decomposition["configuration_mix_effect"])),
                           abs(float(decomposition["within_cohort_quality_effect"])))

    def test_wilson_intervals_express_uncertainty_and_shrink_with_sample(self):
        result = self.compare()
        small = next(s for s in result["strata"] if s["first"]["tested_units"] == 10)
        large = next(s for s in result["strata"] if s["first"]["tested_units"] == 40)
        for stratum in (small, large):
            block = stratum["first"]["interval"]
            self.assertLessEqual(float(block["lower"]), float(block["rate"]) + 1e-9)
            self.assertGreaterEqual(float(block["upper"]), float(block["rate"]) - 1e-9)
        small_width = float(small["first"]["interval"]["upper"]) - float(small["first"]["interval"]["lower"])
        large_width = float(large["first"]["interval"]["upper"]) - float(large["first"]["interval"]["lower"])
        self.assertGreater(small_width, large_width)

    def test_analysis_is_deterministic_and_fingerprinted(self):
        first = self.compare()
        second = self.compare()
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        # Fingerprint covers the full payload.
        payload = {key: value for key, value in first.items() if key != "fingerprint"}
        self.assertEqual(fingerprint(payload), first["fingerprint"])

    def test_lot_composition_change_excludes_cohort_unless_explicitly_allowed(self):
        builder = CohortBuilder(self.service)
        # A second servo lot from the same supplier: the supplier signature is
        # unchanged but the physical lot composition of the premium (rev2) cohort.
        builder.lot("V-PREM-2", "servo", "alpha-motors", 50, day=2)
        builder.complete_job("J-PREMIUM-3", "R2", 10, 15, 9, 1, "P2")
        strict = self.compare()
        premium = next(s for s in strict["strata"]
                       if "alpha-motors" in str(s["supplier_profile"]))
        self.assertFalse(premium["eligible_for_comparison"])
        self.assertIn("lot_composition_changed",
                      [reason["code"] for reason in premium["exclusion_reasons"]])
        relaxed = self.compare(require_same_lots=False)
        premium_relaxed = next(s for s in relaxed["strata"]
                               if "alpha-motors" in str(s["supplier_profile"]))
        self.assertTrue(premium_relaxed["eligible_for_comparison"])

    def test_one_sided_design_revision_is_excluded_and_counted_as_volume(self):
        builder = CohortBuilder(self.service)
        # Revision 3 only appears in window 2 with a distinct part so it cannot
        # pool with either existing cohort; it is one-sided and feeds the volume term.
        builder.lot("V-EXTRA", "servo", "alpha-motors", 50)
        builder.design("R3", 3, parent="R2", parts={"servo": 1, "sensor": 1, "camera": 1})
        builder.lot("C-NEW", "camera", "gamma-optics", 50)
        builder.plan("P3", 3)
        outcome = builder.complete_job("J-R3", "R3", 20, 15, 18, 2, "P3")
        self.assertEqual(outcome["state"], "accepted", outcome)
        result = self.compare()
        rev3 = next(s for s in result["strata"] if s["revision"] == 3)
        self.assertFalse(rev3["eligible_for_comparison"])
        self.assertEqual(rev3["exclusion_reasons"][0]["code"], "one_sided_stratum")
        self.assertNotEqual(result["decomposition"]["volume_effect"], "0.000000")

    def test_unfinished_and_untested_jobs_are_flagged_not_silently_rated(self):
        builder = CohortBuilder(self.service)
        builder.open_unfinished("J-OPEN", "R1", 5, 15)
        result = self.compare()
        gap_codes = {gap["code"] for gap in result["data_gaps"]["second"]}
        self.assertIn("unfinished_job", gap_codes)
        subject = next(gap for gap in result["data_gaps"]["second"]
                       if gap["subject"] == "J-OPEN")
        self.assertEqual(subject["units_without_test"], 5)
        # Untested units never enter the tested pool.
        second = result["coverage"]["second"]
        self.assertEqual(second["untested_units"], 5)
        self.assertEqual(second["unfinished_job_count"], 1)

    def test_min_sample_and_coverage_rules_exclude_thin_cohorts(self):
        # Threshold 50: every window of every stratum stays below the floor,
        # so no controlled comparison can be made and the verdict is inconclusive.
        result = self.service.cohort_compare(
            "robot-arm", W1_START, W1_END, W2_START, W2_END, min_tested_units=50)
        for stratum in result["strata"]:
            self.assertFalse(stratum["eligible_for_comparison"])
            self.assertIn("insufficient_sample",
                          [reason["code"] for reason in stratum["exclusion_reasons"]])
        self.assertEqual(result["interpretation"]["verdict"],
                         "inconclusive_no_comparable_cohort")

    def test_partial_sample_rule_keeps_only_well_supported_cohorts(self):
        # Threshold 20: the 40-unit windows remain eligible, the 10-unit ones drop.
        result = self.service.cohort_compare(
            "robot-arm", W1_START, W1_END, W2_START, W2_END, min_tested_units=20)
        for stratum in result["strata"]:
            thin_window = min(stratum["first"]["tested_units"], stratum["second"]["tested_units"]) < 20
            if thin_window:
                self.assertFalse(stratum["eligible_for_comparison"])
                self.assertIn("insufficient_sample",
                              [reason["code"] for reason in stratum["exclusion_reasons"]])

    def test_rework_jobs_form_separate_strata(self):
        builder = CohortBuilder(self.service)
        # Fail a window-2 job (below the 0.50 plan threshold), then rework it.
        outcome = builder.complete_job("J-FAIL", "R1", 4, 15, 1, 3, "P1")
        self.assertEqual(outcome["state"], "accepted")
        self.assertEqual(self.service.get_job("J-FAIL")["status"], "failed")
        builder.complete_job("J-REWORK", "R1", 4, 16, 4, 0, "P1", parent="J-FAIL")
        result = self.compare()
        rework = [s for s in result["strata"] if s["rework"]]
        self.assertTrue(rework)
        self.assertTrue(any("J-REWORK" in s["second"]["job_ids"] for s in rework))
        self.assertTrue(all(s["first"]["job_count"] == 0 for s in rework))

    def test_frozen_report_reproduces_and_drift_tracks_late_supplemental_test(self):
        first_report = self.service.report("robot-arm", W1_START, W1_END)
        frozen = self.service.publish("w1-freeze", "robot-arm", W1_START, W1_END,
                                      first_report["fingerprint"])
        # Frozen analysis built from the stored release must match a live analysis.
        live = self.compare()
        frozen_report = frozen["report"]
        twin = self.service.cohort_engine._empty_twin(frozen_report)
        frozen_analysis = self.service.cohort_engine.analyze(frozen_report, twin)
        self.assertEqual(frozen_analysis["fingerprint"],
                         self.service.cohort_engine.analyze(
                             self.service.report("robot-arm", W1_START, W1_END),
                             self.service.cohort_engine._empty_twin(first_report))["fingerprint"])
        # Late supplemental test arrives for a previously unfinished job.
        builder = CohortBuilder(self.service)
        builder.open_unfinished("J-LATE", "R1", 8, 8)
        before_drift = self.service.cohort_drift("w1-freeze")
        builder.complete_job("J-LATE", "R1", 8, 17, 7, 1, "P1")
        drift = self.service.cohort_drift("w1-freeze")
        changed_ids = [item["job_id"] for item in drift["job_changes"]["added"]]
        self.assertIn("J-LATE", changed_ids)
        self.assertEqual(drift["frozen_source_head"], before_drift["frozen_source_head"])
        self.assertNotEqual(drift["frozen_analysis_fingerprint"],
                            drift["current_analysis_fingerprint"])
        # The frozen release itself remains byte-stable.
        self.assertEqual(self.service.get_release("w1-freeze")["fingerprint"],
                         frozen["fingerprint"])

    def test_no_testable_jobs_reports_insufficient_data_state(self):
        result = self.service.cohort_compare(
            "robot-arm", "2026-02-01T00:00:00Z", "2026-02-08T00:00:00Z",
            "2026-02-08T00:00:00Z", "2026-02-15T00:00:00Z")
        # Empty windows: coverage explicitly states no testable jobs.
        self.assertIsNone(result["coverage"]["first"]["rate"])
        self.assertEqual(result["coverage"]["first"]["reason"], "no_testable_jobs")
        self.assertEqual(result["interpretation"]["verdict"],
                         "inconclusive_no_comparable_cohort")
