"""
Tests for edgedash/verification.py

One passing case, one failing case, and the trivial-pass case for each check.
All checks are pure functions — zero I/O, no database, no clock.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edgedash.config import Config
from edgedash.verification import (
    CheckResult,
    Verdict,
    check_extraction_sanity,
    check_freshness,
    check_gap_sample_size,
    check_score_spread,
    run_all_checks,
)


# ---------------------------------------------------------------------------
# Shared fixture builder
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> Config:
    """Minimal Config with verification thresholds; override per test."""
    defaults = dict(
        target_role="Data Analyst",
        target_city="Bengaluru",
        keywords=[],
        my_skills=[],
        experience_years=3,
        db_path=":memory:",
        min_fit_score=60,
        sources=["arbeitnow"],
        use_mock_fetcher=False,
        llm_provider="gemini",
        llm_model="gemini-1.5-flash",
        score_batch_size=25,
        fetch_interval_hours=6,
        max_score_seconds=300,
        max_analyse_seconds=120,
        max_fetch_pages=5,
        max_fetch_listings=200,
        target_seniority="mid",
        score_weights={
            "skill_match": 0.45,
            "seniority_fit": 0.25,
            "location_fit": 0.15,
            "recency": 0.15,
        },
        skill_aliases={},
        # verification thresholds
        min_score_spread=10,
        min_score_stdev=5,
        max_empty_extraction_pct=20.0,
        max_skills_per_listing=20,
        min_gap_sample=3,
        max_data_age_days=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# check_score_spread
# ---------------------------------------------------------------------------

class TestCheckScoreSpread(unittest.TestCase):

    # --- trivial pass: fewer than 5 scores ---

    def test_fewer_than_five_scores_passes_trivially(self) -> None:
        result = check_score_spread([70, 80, 60], _cfg())
        self.assertTrue(result.passed)
        self.assertIn("trivially", result.message)
        self.assertIn("3", result.message)

    def test_zero_scores_passes_trivially(self) -> None:
        result = check_score_spread([], _cfg())
        self.assertTrue(result.passed)
        self.assertIn("trivially", result.message)

    def test_four_scores_passes_trivially(self) -> None:
        result = check_score_spread([10, 20, 30, 40], _cfg())
        self.assertTrue(result.passed)

    # --- passing case: spread and stdev both above thresholds ---

    def test_well_distributed_scores_pass(self) -> None:
        # spread = 90-10 = 80, stdev well above 5
        scores = [10, 30, 50, 70, 90]
        result = check_score_spread(scores, _cfg())
        self.assertTrue(result.passed)
        self.assertIn("passed", result.message)
        self.assertIn("spread=80", result.observed)

    def test_passes_at_exact_thresholds(self) -> None:
        # spread = exactly 10, stdev must be >= 5
        # scores [45, 50, 50, 50, 55] → spread=10, stdev≈3.5 → stdev fails
        # Use [40, 48, 50, 52, 60] → spread=20, stdev≈7.5 → both pass
        scores = [40, 48, 50, 52, 60]
        result = check_score_spread(scores, _cfg())
        self.assertTrue(result.passed)

    # --- failing case: narrow spread ---

    def test_narrow_spread_fails(self) -> None:
        # All scores between 69-71: spread=2, stdev < 1
        scores = [69, 70, 70, 71, 70]
        result = check_score_spread(scores, _cfg())
        self.assertFalse(result.passed)
        self.assertIn("FAILED", result.message)
        self.assertIn("spread=2", result.message)

    def test_failure_names_both_offending_values(self) -> None:
        scores = [69, 70, 70, 71, 70]
        result = check_score_spread(scores, _cfg())
        # Message must name observed spread AND observed stdev (rule 37)
        self.assertIn("spread=", result.message)
        self.assertIn("stdev=", result.message)

    def test_low_stdev_fails_even_with_adequate_spread(self) -> None:
        # scores [60,60,60,60,70]: spread=10 (ok), stdev≈4.5 (fail)
        scores = [60, 60, 60, 60, 70]
        result = check_score_spread(scores, _cfg(min_score_spread=10, min_score_stdev=5))
        self.assertFalse(result.passed)
        self.assertIn("stdev=", result.message)

    def test_check_name_is_score_spread(self) -> None:
        result = check_score_spread([10, 30, 50, 70, 90], _cfg())
        self.assertEqual(result.name, "score_spread")

    def test_observed_contains_spread_and_stdev(self) -> None:
        scores = [10, 30, 50, 70, 90]
        result = check_score_spread(scores, _cfg())
        self.assertIn("spread=", result.observed)
        self.assertIn("stdev=", result.observed)


# ---------------------------------------------------------------------------
# check_extraction_sanity
# ---------------------------------------------------------------------------

class TestCheckExtractionSanity(unittest.TestCase):

    # --- trivial pass: no extractions ---

    def test_empty_facts_list_passes_trivially(self) -> None:
        result = check_extraction_sanity([], _cfg())
        self.assertTrue(result.passed)
        self.assertIn("trivially", result.message)

    # --- passing case ---

    def test_healthy_extractions_pass(self) -> None:
        facts = [
            {"required_skills": ["Python", "SQL"]},
            {"required_skills": ["Excel", "Power BI", "Python"]},
            {"required_skills": ["SQL", "Tableau"]},
            {"required_skills": ["Python", "Pandas", "NumPy"]},
            {"required_skills": ["R", "Statistics"]},
        ]
        result = check_extraction_sanity(facts, _cfg())
        self.assertTrue(result.passed)
        self.assertIn("passed", result.message)

    def test_one_empty_out_of_ten_passes(self) -> None:
        # 10% empty < 20% threshold
        facts = [{"required_skills": []}] + [
            {"required_skills": ["Python"]} for _ in range(9)
        ]
        result = check_extraction_sanity(facts, _cfg())
        self.assertTrue(result.passed)

    # --- failing case: too many empty extractions ---

    def test_majority_empty_fails(self) -> None:
        # 3 empty out of 5 = 60% > 20% threshold
        facts = [
            {"required_skills": []},
            {"required_skills": []},
            {"required_skills": []},
            {"required_skills": ["Python"]},
            {"required_skills": ["SQL"]},
        ]
        result = check_extraction_sanity(facts, _cfg())
        self.assertFalse(result.passed)
        self.assertIn("FAILED", result.message)
        self.assertIn("empty_pct=60.0%", result.message)

    def test_failure_names_empty_pct_threshold(self) -> None:
        facts = [{"required_skills": []}] * 10
        result = check_extraction_sanity(facts, _cfg())
        # Must name the observed value and the threshold (rule 37)
        self.assertIn("empty_pct=100.0%", result.message)
        self.assertIn("max_empty_extraction_pct=20%", result.message)

    # --- failing case: too many skills per listing ---

    def test_oversized_skills_list_fails(self) -> None:
        # 21 skills on one listing > threshold of 20
        fat_listing = {"required_skills": [f"skill_{i}" for i in range(21)]}
        facts = [fat_listing] + [{"required_skills": ["Python"]}] * 4
        result = check_extraction_sanity(facts, _cfg())
        self.assertFalse(result.passed)
        self.assertIn("FAILED", result.message)
        self.assertIn("max_skills_per_listing=21", result.message)

    def test_exactly_at_max_skills_passes(self) -> None:
        # Exactly 20 skills = threshold, should pass
        listing = {"required_skills": [f"skill_{i}" for i in range(20)]}
        facts = [listing] + [{"required_skills": ["Python"]}] * 4
        result = check_extraction_sanity(facts, _cfg())
        self.assertTrue(result.passed)

    def test_missing_required_skills_key_treated_as_empty(self) -> None:
        # A dict without 'required_skills' key counts as empty
        facts = [{}] * 10
        result = check_extraction_sanity(facts, _cfg(max_empty_extraction_pct=20))
        self.assertFalse(result.passed)

    def test_check_name_is_extraction_sanity(self) -> None:
        result = check_extraction_sanity([], _cfg())
        self.assertEqual(result.name, "extraction_sanity")


# ---------------------------------------------------------------------------
# check_gap_sample_size
# ---------------------------------------------------------------------------

class TestCheckGapSampleSize(unittest.TestCase):

    def test_empty_gaps_passes_trivially(self) -> None:
        result = check_gap_sample_size([], _cfg())
        self.assertTrue(result.passed)
        self.assertIn("trivially", result.message)

    def test_top_gap_meets_threshold_passes(self) -> None:
        gaps = [
            {"skill": "dbt", "listings_blocked": 5, "opportunity_cost": 3.2},
            {"skill": "Airflow", "listings_blocked": 2, "opportunity_cost": 1.1},
        ]
        result = check_gap_sample_size(gaps, _cfg())
        self.assertTrue(result.passed)

    def test_top_gap_at_exact_threshold_passes(self) -> None:
        gaps = [{"skill": "Spark", "listings_blocked": 3, "opportunity_cost": 2.0}]
        result = check_gap_sample_size(gaps, _cfg(min_gap_sample=3))
        self.assertTrue(result.passed)

    def test_top_gap_below_threshold_fails(self) -> None:
        gaps = [
            {"skill": "Rust", "listings_blocked": 1, "opportunity_cost": 0.8},
            {"skill": "SQL", "listings_blocked": 8, "opportunity_cost": 5.0},
        ]
        result = check_gap_sample_size(gaps, _cfg())
        self.assertFalse(result.passed)
        self.assertIn("FAILED", result.message)
        self.assertIn("listings_blocked=1", result.message)
        self.assertIn("Rust", result.message)

    def test_failure_names_threshold(self) -> None:
        gaps = [{"skill": "Rust", "listings_blocked": 2, "opportunity_cost": 0.8}]
        result = check_gap_sample_size(gaps, _cfg(min_gap_sample=3))
        self.assertIn("min_gap_sample=3", result.message)

    def test_check_name_is_gap_sample_size(self) -> None:
        result = check_gap_sample_size([], _cfg())
        self.assertEqual(result.name, "gap_sample_size")


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------

class TestCheckFreshness(unittest.TestCase):

    def test_fresh_data_passes(self) -> None:
        # 1 day old — well within max_data_age_days=3
        fetch_at = "2026-08-20T12:00:00+00:00"
        result = check_freshness(fetch_at, _cfg(), _NOW)
        self.assertTrue(result.passed)
        self.assertIn("passed", result.message)

    def test_exactly_at_threshold_passes(self) -> None:
        # Exactly 3 days old — should pass (age <= threshold)
        fetch_at = "2026-08-18T12:00:00+00:00"
        result = check_freshness(fetch_at, _cfg(max_data_age_days=3), _NOW)
        self.assertTrue(result.passed)

    def test_stale_data_fails(self) -> None:
        # 5 days old > threshold of 3
        fetch_at = "2026-08-16T12:00:00+00:00"
        result = check_freshness(fetch_at, _cfg(), _NOW)
        self.assertFalse(result.passed)
        self.assertIn("FAILED", result.message)
        self.assertIn("5.", result.message)   # age_days ~5.0

    def test_failure_names_age_and_threshold(self) -> None:
        fetch_at = "2026-08-16T12:00:00+00:00"
        result = check_freshness(fetch_at, _cfg(max_data_age_days=3), _NOW)
        # Must name observed age AND threshold (rule 37)
        self.assertIn("age=", result.message)
        self.assertIn("max_data_age_days=3", result.message)

    def test_none_fetch_at_fails(self) -> None:
        result = check_freshness(None, _cfg(), _NOW)
        self.assertFalse(result.passed)
        self.assertIn("None", result.message)

    def test_unparseable_timestamp_fails(self) -> None:
        result = check_freshness("not-a-date", _cfg(), _NOW)
        self.assertFalse(result.passed)
        self.assertIn("unparseable", result.message)

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        # Naive ISO string (no tz) should not raise
        fetch_at = "2026-08-20T12:00:00"
        result = check_freshness(fetch_at, _cfg(), _NOW)
        # 1 day old naive timestamp treated as UTC → passes
        self.assertTrue(result.passed)

    def test_check_name_is_freshness(self) -> None:
        result = check_freshness("2026-08-20T12:00:00+00:00", _cfg(), _NOW)
        self.assertEqual(result.name, "freshness")


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------

class TestRunAllChecks(unittest.TestCase):

    def _healthy_inputs(self):
        scores      = [10, 30, 50, 70, 90]
        facts_list  = [{"required_skills": ["Python", "SQL"]}] * 5
        gaps        = [{"skill": "dbt", "listings_blocked": 5, "opportunity_cost": 3.0}]
        fetch_at    = "2026-08-20T12:00:00+00:00"
        return scores, facts_list, gaps, fetch_at

    def test_all_passing_inputs_produce_pass_verdict(self) -> None:
        scores, facts_list, gaps, fetch_at = self._healthy_inputs()
        verdict = run_all_checks(scores, facts_list, gaps, fetch_at, _cfg(), _NOW)
        self.assertTrue(verdict.passed)
        self.assertEqual(len(verdict.failed_checks), 0)

    def test_one_failing_check_produces_fail_verdict(self) -> None:
        scores, facts_list, gaps, _ = self._healthy_inputs()
        # Stale data will cause freshness to fail
        stale_fetch = "2026-08-10T12:00:00+00:00"
        verdict = run_all_checks(scores, facts_list, gaps, stale_fetch, _cfg(), _NOW)
        self.assertFalse(verdict.passed)
        self.assertEqual(len(verdict.failed_checks), 1)
        self.assertEqual(verdict.failed_checks[0].name, "freshness")

    def test_all_four_checks_always_run(self) -> None:
        scores, facts_list, gaps, fetch_at = self._healthy_inputs()
        verdict = run_all_checks(scores, facts_list, gaps, fetch_at, _cfg(), _NOW)
        self.assertEqual(len(verdict.all_checks), 4)

    def test_verdict_summary_pass_format(self) -> None:
        scores, facts_list, gaps, fetch_at = self._healthy_inputs()
        verdict = run_all_checks(scores, facts_list, gaps, fetch_at, _cfg(), _NOW)
        self.assertIn("PASS", verdict.summary)
        self.assertIn("4 checks", verdict.summary)

    def test_verdict_summary_fail_names_failing_check(self) -> None:
        stale = "2026-08-10T12:00:00+00:00"
        scores, facts, gaps, _ = self._healthy_inputs()
        verdict = run_all_checks(scores, facts, gaps, stale, _cfg(), _NOW)
        self.assertIn("FAIL", verdict.summary)
        self.assertIn("freshness", verdict.summary)

    def test_multiple_failures_all_appear_in_verdict(self) -> None:
        # Narrow scores + stale data → two checks fail
        scores    = [69, 70, 70, 71, 70]
        facts     = [{"required_skills": ["Python"]}] * 5
        gaps      = [{"skill": "dbt", "listings_blocked": 5, "opportunity_cost": 3.0}]
        stale     = "2026-08-10T12:00:00+00:00"
        verdict   = run_all_checks(scores, facts, gaps, stale, _cfg(), _NOW)
        self.assertFalse(verdict.passed)
        self.assertGreaterEqual(len(verdict.failed_checks), 2)

    def test_failed_checks_are_subset_of_all_checks(self) -> None:
        scores, facts, gaps, fetch_at = self._healthy_inputs()
        verdict = run_all_checks(scores, facts, gaps, fetch_at, _cfg(), _NOW)
        for fc in verdict.failed_checks:
            self.assertIn(fc, verdict.all_checks)


if __name__ == "__main__":
    unittest.main()
