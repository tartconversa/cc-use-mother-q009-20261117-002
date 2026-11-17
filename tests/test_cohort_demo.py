import unittest

from cycleflow.cohort_demo import run


class CohortDemoTests(unittest.TestCase):
    def test_demo_runs_and_keeps_frozen_report_stable(self):
        result = run()
        self.assertEqual(result["before_late_event"]["verdict"],
                         "improvement_within_comparable_cohorts")
        # The decomposition must explain the headline 0.64 -> 0.86 movement.
        decomposition = result["before_late_event"]["decomposition"]
        self.assertEqual(decomposition["volume"], "0.000000")
        self.assertNotEqual(decomposition["configuration_mix"], "0.000000")
        self.assertNotEqual(decomposition["within_quality"], "0.000000")
        # Late supplemental test is tracked while the frozen release stays byte-stable.
        self.assertEqual(result["drift"]["jobs_added"], 1)
        self.assertTrue(result["drift"]["frozen_release_unchanged"])
        self.assertGreater(result["drift"]["current_head"], result["drift"]["frozen_head"])
        # The late event raised week-1 coverage to complete.
        self.assertEqual(result["after_late_event"]["coverage_first"]["untested_units"], 0)


if __name__ == "__main__":
    unittest.main()
