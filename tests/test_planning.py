"""
Tests for build_plan (edgedash/planning.py).

Four scenarios, zero I/O — build_plan is a pure function of (state, config).
SystemState instances are constructed directly; no database is touched.

Scenarios
---------
1. everything_stale        — all three agents RUN
2. nothing_to_do           — all three agents SKIP
3. only_unscored           — fetch SKIP, score RUN, analyse SKIP
4. gaps_stale_nothing_new  — fetch SKIP, score SKIP, analyse RUN
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
from edgedash.planning import Decision, build_plan
from edgedash.state import SystemState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)

# A timestamp 8 hours before _NOW — fetch interval expired (default 6 h)
_STALE_FETCH = "2026-08-21T04:00:00+00:00"

# A timestamp 2 hours before _NOW — fetch interval NOT expired
_FRESH_FETCH = "2026-08-21T10:00:00+00:00"

# A gap snapshot computed before the most recent score
_OLD_GAPS = "2026-08-21T09:00:00+00:00"
_RECENT_SCORE = "2026-08-21T11:00:00+00:00"   # scored after _OLD_GAPS → stale

# A gap snapshot computed after the most recent score
_FRESH_GAPS = "2026-08-21T11:30:00+00:00"
_OLDER_SCORE = "2026-08-21T10:00:00+00:00"    # scored before _FRESH_GAPS → not stale


def _make_config(**overrides) -> Config:
    """Return a minimal Config suitable for planning tests."""
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
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_state(**overrides) -> SystemState:
    """Return a SystemState with sensible defaults, overridable per test."""
    defaults = dict(
        observed_at=_NOW,
        last_fetch_at=_FRESH_FETCH,
        hours_since_fetch=2.0,
        unscored_count=0,
        gaps_computed_at=_FRESH_GAPS,
        gaps_stale=False,
        last_cycle_verdict="ok",
        last_cycle_at="2026-08-21T10:30:00+00:00",
    )
    defaults.update(overrides)
    return SystemState(**defaults)


# ---------------------------------------------------------------------------
# Scenario 1: everything stale — all three agents RUN
# ---------------------------------------------------------------------------

class TestEverythingStale(unittest.TestCase):

    def setUp(self) -> None:
        state = _make_state(
            last_fetch_at=_STALE_FETCH,
            hours_since_fetch=8.0,   # > 6 h threshold → fetch runs
            unscored_count=41,       # > 0 → scorer runs
            gaps_computed_at=_OLD_GAPS,
            gaps_stale=True,         # scored after gap snapshot → analyser runs
        )
        self.plan = build_plan(state, _make_config())

    def test_three_tasks_in_plan(self) -> None:
        self.assertEqual(len(self.plan), 3)

    def test_all_agents_run(self) -> None:
        self.assertEqual(len(self.plan.runs()), 3)
        self.assertEqual(len(self.plan.skips()), 0)

    def test_agent_order(self) -> None:
        names = [t.agent for t in self.plan]
        self.assertEqual(names, ["fetcher", "scorer", "gap_analyzer"])

    def test_fetcher_reason_contains_hours(self) -> None:
        fetcher = self.plan.tasks[0]
        self.assertIn("8.0", fetcher.reason)
        self.assertIn("threshold=6", fetcher.reason)

    def test_scorer_reason_contains_count(self) -> None:
        scorer = self.plan.tasks[1]
        self.assertIn("unscored_count=41", scorer.reason)

    def test_analyser_reason_mentions_stale(self) -> None:
        analyser = self.plan.tasks[2]
        self.assertIn("gaps_stale=True", analyser.reason)

    def test_fetcher_stop_conditions_set(self) -> None:
        stop = self.plan.tasks[0].stop
        self.assertEqual(stop.max_pages, 5)
        self.assertEqual(stop.max_listings, 200)

    def test_scorer_stop_conditions_set(self) -> None:
        stop = self.plan.tasks[1].stop
        self.assertEqual(stop.max_items, 25)
        self.assertEqual(stop.max_seconds, 300)

    def test_analyser_stop_conditions_set(self) -> None:
        stop = self.plan.tasks[2].stop
        self.assertEqual(stop.max_seconds, 120)

    def test_render_contains_all_agents(self) -> None:
        rendered = self.plan.render()
        for agent in ("fetcher", "scorer", "gap_analyzer"):
            self.assertIn(agent, rendered)

    def test_render_shows_run_tags(self) -> None:
        rendered = self.plan.render()
        self.assertIn("[RUN", rendered)
        self.assertNotIn("[SKIP]", rendered)


# ---------------------------------------------------------------------------
# Scenario 2: nothing to do — all three agents SKIP
# ---------------------------------------------------------------------------

class TestNothingToDo(unittest.TestCase):

    def setUp(self) -> None:
        state = _make_state(
            last_fetch_at=_FRESH_FETCH,
            hours_since_fetch=2.0,   # < 6 h → fetch skipped
            unscored_count=0,        # nothing to score
            gaps_computed_at=_FRESH_GAPS,
            gaps_stale=False,        # gaps up to date
        )
        self.plan = build_plan(state, _make_config())

    def test_all_agents_skipped(self) -> None:
        self.assertEqual(len(self.plan.skips()), 3)
        self.assertEqual(len(self.plan.runs()), 0)

    def test_three_tasks_still_present(self) -> None:
        # Skipped agents must not be silently absent (rule 31)
        self.assertEqual(len(self.plan), 3)

    def test_agent_order_preserved(self) -> None:
        names = [t.agent for t in self.plan]
        self.assertEqual(names, ["fetcher", "scorer", "gap_analyzer"])

    def test_all_decisions_are_skip(self) -> None:
        for task in self.plan:
            self.assertEqual(task.decision, Decision.SKIP)

    def test_skip_reasons_are_informative(self) -> None:
        for task in self.plan:
            self.assertIn("skipped", task.reason.lower())
            self.assertGreater(len(task.reason), 10)

    def test_fetcher_skip_reason_contains_hours(self) -> None:
        self.assertIn("2.0", self.plan.tasks[0].reason)

    def test_scorer_skip_reason_contains_count(self) -> None:
        self.assertIn("unscored_count=0", self.plan.tasks[1].reason)

    def test_analyser_skip_reason_contains_gaps(self) -> None:
        self.assertIn("gaps_stale=False", self.plan.tasks[2].reason)

    def test_render_shows_skip_tags(self) -> None:
        rendered = self.plan.render()
        self.assertIn("[SKIP]", rendered)
        self.assertNotIn("[RUN", rendered)

    def test_rendered_plan_for_nothing_to_do(self) -> None:
        """Snapshot the rendered output so we can verify the format visually."""
        rendered = self.plan.render()
        # Must contain all three agents, must have SKIP tags, must have reasons
        self.assertEqual(rendered.count("[SKIP]"), 3)
        self.assertIn("fetcher", rendered)
        self.assertIn("scorer", rendered)
        self.assertIn("gap_analyzer", rendered)


# ---------------------------------------------------------------------------
# Scenario 3: only unscored listings — fetch SKIP, score RUN, analyse SKIP
# ---------------------------------------------------------------------------

class TestOnlyUnscored(unittest.TestCase):

    def setUp(self) -> None:
        state = _make_state(
            last_fetch_at=_FRESH_FETCH,
            hours_since_fetch=2.0,   # fetch not due
            unscored_count=15,       # scorer should run
            gaps_computed_at=_FRESH_GAPS,
            gaps_stale=False,        # gaps not stale yet
        )
        self.plan = build_plan(state, _make_config())

    def test_fetcher_skipped(self) -> None:
        self.assertEqual(self.plan.tasks[0].decision, Decision.SKIP)

    def test_scorer_runs(self) -> None:
        self.assertEqual(self.plan.tasks[1].decision, Decision.RUN)

    def test_analyser_skipped(self) -> None:
        # gaps_stale=False and unscored_count>0 does not trigger analyser
        self.assertEqual(self.plan.tasks[2].decision, Decision.SKIP)

    def test_scorer_goal_mentions_batch_size(self) -> None:
        goal = self.plan.tasks[1].goal
        self.assertIn("25", goal)

    def test_scorer_reason_contains_count(self) -> None:
        self.assertIn("unscored_count=15", self.plan.tasks[1].reason)

    def test_run_count(self) -> None:
        self.assertEqual(len(self.plan.runs()), 1)
        self.assertEqual(self.plan.runs()[0].agent, "scorer")


# ---------------------------------------------------------------------------
# Scenario 4: gaps stale, nothing unscored — fetch SKIP, score SKIP, analyse RUN
# ---------------------------------------------------------------------------

class TestGapsStaleNothingNew(unittest.TestCase):

    def setUp(self) -> None:
        state = _make_state(
            last_fetch_at=_FRESH_FETCH,
            hours_since_fetch=2.0,   # fetch not due
            unscored_count=0,        # nothing to score
            gaps_computed_at=_OLD_GAPS,
            gaps_stale=True,         # a listing was scored after the gap snapshot
        )
        self.plan = build_plan(state, _make_config())

    def test_fetcher_skipped(self) -> None:
        self.assertEqual(self.plan.tasks[0].decision, Decision.SKIP)

    def test_scorer_skipped(self) -> None:
        self.assertEqual(self.plan.tasks[1].decision, Decision.SKIP)

    def test_analyser_runs(self) -> None:
        self.assertEqual(self.plan.tasks[2].decision, Decision.RUN)

    def test_analyser_reason_mentions_stale(self) -> None:
        self.assertIn("gaps_stale=True", self.plan.tasks[2].reason)

    def test_analyser_stop_has_max_seconds(self) -> None:
        self.assertEqual(self.plan.tasks[2].stop.max_seconds, 120)

    def test_run_count(self) -> None:
        self.assertEqual(len(self.plan.runs()), 1)
        self.assertEqual(self.plan.runs()[0].agent, "gap_analyzer")


# ---------------------------------------------------------------------------
# Edge case: first ever run — never fetched, no gaps, no scores
# ---------------------------------------------------------------------------

class TestFirstEverRun(unittest.TestCase):

    def setUp(self) -> None:
        state = _make_state(
            last_fetch_at=None,
            hours_since_fetch=None,  # never fetched
            unscored_count=0,
            gaps_computed_at=None,   # never computed
            gaps_stale=True,         # None → stale=True by definition
            last_cycle_verdict=None,
            last_cycle_at=None,
        )
        self.plan = build_plan(state, _make_config())

    def test_fetcher_runs_on_first_ever_run(self) -> None:
        self.assertEqual(self.plan.tasks[0].decision, Decision.RUN)

    def test_fetcher_reason_says_never(self) -> None:
        self.assertIn("never", self.plan.tasks[0].reason.lower())

    def test_analyser_runs_when_no_gaps_yet(self) -> None:
        self.assertEqual(self.plan.tasks[2].decision, Decision.RUN)

    def test_analyser_reason_says_never(self) -> None:
        self.assertIn("never", self.plan.tasks[2].reason.lower())

    def test_scorer_skipped_with_zero_unscored(self) -> None:
        self.assertEqual(self.plan.tasks[1].decision, Decision.SKIP)


if __name__ == "__main__":
    unittest.main()
