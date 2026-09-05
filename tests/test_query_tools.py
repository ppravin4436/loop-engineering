"""
Unit tests for deterministic query tool registry in edgedash/query/tools.py (rules 40–46).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edgedash import storage
from edgedash.config import Config
from edgedash.query.tools import (
    TOOLS,
    _clamp_int,
    best_matches,
    companies_hiring,
    gap_detail,
    listing_count,
    skill_demand,
    top_gaps,
    trend,
)


def _make_cfg(db_path: str) -> Config:
    return Config(
        db_path=db_path,
        skill_aliases={
            "k8s": "kubernetes",
            "postgres": "postgresql",
            "py": "python",
        },
    )


def _seed_verified_cycle(db_path: str) -> None:
    """Log a passing cycle_summary row so rule 46 guard passes."""
    storage.log_cycle(
        db_path=db_path,
        agent="cycle_summary",
        started_at="2026-09-01T10:00:00+00:00",
        finished_at="2026-09-01T10:05:00+00:00",
        records_touched=5,
        status="ok",
        notes="verdict=ok",
    )


class TestQueryToolRegistry(unittest.TestCase):

    def test_registry_contains_all_seven_tools(self) -> None:
        expected_tools = {
            "companies_hiring",
            "best_matches",
            "top_gaps",
            "gap_detail",
            "trend",
            "listing_count",
            "skill_demand",
        }
        self.assertEqual(set(TOOLS.keys()), expected_tools)

    def test_tool_specs_have_valid_metadata(self) -> None:
        for name, spec in TOOLS.items():
            self.assertEqual(spec.name, name)
            self.assertTrue(len(spec.description) > 20, f"Description for {name} is too short")
            self.assertEqual(spec.parameters.get("type"), "object")
            self.assertTrue(callable(spec.func))


class TestClamping(unittest.TestCase):

    def test_clamp_int_boundaries(self) -> None:
        # Lower bound
        self.assertEqual(_clamp_int(-10, default=7, min_val=1, max_val=90), 1)
        self.assertEqual(_clamp_int(0, default=7, min_val=1, max_val=90), 1)
        # Upper bound
        self.assertEqual(_clamp_int(100, default=7, min_val=1, max_val=90), 90)
        # Valid value
        self.assertEqual(_clamp_int(14, default=7, min_val=1, max_val=90), 14)
        # Invalid / non-integer input
        self.assertEqual(_clamp_int("invalid", default=7, min_val=1, max_val=90), 7)
        self.assertEqual(_clamp_int(None, default=7, min_val=1, max_val=90), 7)


class TestQueryTools(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test_query.db")
        storage.init_db(self.db_path)
        self.cfg = _make_cfg(self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_rule_46_guard_blocks_unverified_db(self) -> None:
        # With no cycle_summary logged, all tools must return 0 rows and a safety summary
        res = companies_hiring(7, config=self.cfg)
        self.assertEqual(res["rows"], [])
        self.assertIn("No verified cycle exists", res["summary"])

        res2 = best_matches(5, config=self.cfg)
        self.assertEqual(res2["rows"], [])
        self.assertIn("No verified cycle exists", res2["summary"])

    def test_companies_hiring(self) -> None:
        _seed_verified_cycle(self.db_path)

        now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
        # Insert test listings
        listings = [
            {
                "id": "l1", "title": "Analyst 1", "company": "Acme Corp", "location": "Remote",
                "url": "http://example.com/1", "description": "desc", "source": "test",
                "posted_at": "2026-09-02T10:00:00+00:00", "fetched_at": "2026-09-02T12:00:00+00:00",
                "fit_score": 85, "fit_reason": "good",
            },
            {
                "id": "l2", "title": "Analyst 2", "company": "Acme Corp", "location": "Remote",
                "url": "http://example.com/2", "description": "desc", "source": "test",
                "posted_at": "2026-09-01T10:00:00+00:00", "fetched_at": "2026-09-01T12:00:00+00:00",
                "fit_score": 80, "fit_reason": "good",
            },
            {
                "id": "l3", "title": "Engineer", "company": "Beta Tech", "location": "Remote",
                "url": "http://example.com/3", "description": "desc", "source": "test",
                "posted_at": "2026-08-10T10:00:00+00:00", "fetched_at": "2026-08-10T12:00:00+00:00",
                "fit_score": 75, "fit_reason": "good",
            },
        ]
        storage.upsert_listings(self.db_path, listings)

        # Query last 3 days
        res = companies_hiring(days=3, config=self.cfg, now=now)
        self.assertIsInstance(res["rows"], list)
        self.assertEqual(len(res["rows"]), 1)
        self.assertEqual(res["rows"][0]["company"], "Acme Corp")
        self.assertEqual(res["rows"][0]["listing_count"], 2)
        self.assertIn("Found 1 companies across 2 listings", res["summary"])

        # Clamping check: days=150 clamped to 90 covers Beta Tech too
        res_all = companies_hiring(days=150, config=self.cfg, now=now)
        self.assertEqual(len(res_all["rows"]), 2)

    def test_best_matches(self) -> None:
        _seed_verified_cycle(self.db_path)
        listings = [
            {
                "id": f"l{i}", "title": f"Role {i}", "company": "Test Co", "location": "Remote",
                "url": f"http://example.com/{i}", "description": "desc", "source": "test",
                "posted_at": "2026-09-01T10:00:00+00:00", "fetched_at": "2026-09-01T12:00:00+00:00",
                "fit_score": 50 + i * 5, "fit_reason": f"reason {i}",
            }
            for i in range(1, 6)
        ]
        storage.upsert_listings(self.db_path, listings)

        res = best_matches(n=3, config=self.cfg)
        self.assertEqual(len(res["rows"]), 3)
        self.assertEqual(res["rows"][0]["fit_score"], 75)
        self.assertEqual(res["rows"][1]["fit_score"], 70)
        self.assertIn("Top 3 matching listing(s)", res["summary"])

    def test_top_gaps(self) -> None:
        _seed_verified_cycle(self.db_path)
        gaps = [
            {"skill": "kubernetes", "listings_blocked": 5, "opportunity_cost": 4.2, "mean_score": 84.0, "example_ids": ["l1"], "also_nice_to_have": 1},
            {"skill": "docker", "listings_blocked": 4, "opportunity_cost": 3.1, "mean_score": 77.5, "example_ids": ["l2"], "also_nice_to_have": 0},
            {"skill": "spark", "listings_blocked": 2, "opportunity_cost": 1.8, "mean_score": 90.0, "example_ids": ["l3"], "also_nice_to_have": 0},
        ]
        storage.save_gap_snapshot(self.db_path, "run_1", "2026-09-01T10:00:00+00:00", gaps)

        res = top_gaps(n=2, config=self.cfg)
        self.assertEqual(len(res["rows"]), 2)
        self.assertEqual(res["rows"][0]["skill"], "kubernetes")
        self.assertEqual(res["rows"][1]["skill"], "docker")
        self.assertIn("kubernetes", res["summary"])

    def test_gap_detail_known_and_unknown(self) -> None:
        _seed_verified_cycle(self.db_path)
        listings = [
            {
                "id": "l1", "title": "Cloud Eng", "company": "Alpha", "location": "Remote",
                "url": "http://example.com/1", "description": "desc", "source": "test",
                "posted_at": "2026-09-01T10:00:00+00:00", "fetched_at": "2026-09-01T12:00:00+00:00",
                "fit_score": 88, "fit_reason": "good",
            }
        ]
        storage.upsert_listings(self.db_path, listings)
        gaps = [
            {"skill": "kubernetes", "listings_blocked": 1, "opportunity_cost": 0.88, "mean_score": 88.0, "example_ids": ["l1"], "also_nice_to_have": 0},
        ]
        storage.save_gap_snapshot(self.db_path, "run_1", "2026-09-01T10:00:00+00:00", gaps)

        # Alias lookup (k8s -> kubernetes)
        res_alias = gap_detail(skill="k8s", config=self.cfg)
        self.assertEqual(len(res_alias["rows"]), 1)
        self.assertEqual(res_alias["rows"][0]["id"], "l1")
        self.assertIn("kubernetes", res_alias["summary"])

        # Unknown skill does not crash, returns empty rows + clean message
        res_unknown = gap_detail(skill="nonexistent_skill", config=self.cfg)
        self.assertEqual(res_unknown["rows"], [])
        self.assertIn("not a tracked gap", res_unknown["summary"])

    def test_trend(self) -> None:
        _seed_verified_cycle(self.db_path)
        now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        # Snapshot 1 (earlier)
        gaps_1 = [
            {"skill": "kubernetes", "listings_blocked": 3, "opportunity_cost": 2.4, "mean_score": 80.0, "example_ids": ["1"], "also_nice_to_have": 0},
            {"skill": "docker", "listings_blocked": 2, "opportunity_cost": 1.6, "mean_score": 80.0, "example_ids": ["2"], "also_nice_to_have": 0},
        ]
        storage.save_gap_snapshot(self.db_path, "run_1", "2026-08-20T10:00:00+00:00", gaps_1)

        # Snapshot 2 (latest)
        gaps_2 = [
            {"skill": "kubernetes", "listings_blocked": 5, "opportunity_cost": 4.0, "mean_score": 80.0, "example_ids": ["1"], "also_nice_to_have": 0},
            {"skill": "airflow", "listings_blocked": 2, "opportunity_cost": 1.5, "mean_score": 75.0, "example_ids": ["3"], "also_nice_to_have": 0},
        ]
        storage.save_gap_snapshot(self.db_path, "run_2", "2026-09-01T10:00:00+00:00", gaps_2)

        res = trend(weeks=3, config=self.cfg, now=now)
        self.assertIsInstance(res["rows"], list)
        skills = {r["skill"]: r for r in res["rows"]}

        # kubernetes increased by 1.6
        self.assertIn("kubernetes", skills)
        self.assertEqual(skills["kubernetes"]["cost_change"], 1.6)
        self.assertEqual(skills["kubernetes"]["status"], "INCREASING")

        # airflow is NEW
        self.assertIn("airflow", skills)
        self.assertEqual(skills["airflow"]["status"], "NEW")

        # docker DROPPED
        self.assertIn("docker", skills)
        self.assertEqual(skills["docker"]["status"], "DROPPED")

    def test_listing_count(self) -> None:
        _seed_verified_cycle(self.db_path)
        listings = [
            {
                "id": "l1", "title": "T1", "company": "C1", "location": "L1", "url": "U1",
                "description": "d", "source": "s", "posted_at": "2026-09-01T00:00:00+00:00",
                "fetched_at": "2026-09-01T12:00:00+00:00", "fit_score": 80, "fit_reason": "r",
            },
            {
                "id": "l2", "title": "T2", "company": "C2", "location": "L2", "url": "U2",
                "description": "d", "source": "s", "posted_at": "2026-09-01T00:00:00+00:00",
                "fetched_at": "2026-09-01T12:00:00+00:00", "fit_score": None, "fit_reason": None,
            },
        ]
        storage.upsert_listings(self.db_path, listings)

        res = listing_count(config=self.cfg)
        self.assertEqual(len(res["rows"]), 1)
        row = res["rows"][0]
        self.assertEqual(row["total_listings"], 2)
        self.assertEqual(row["scored_listings"], 1)
        self.assertEqual(row["unscored_listings"], 1)
        self.assertIn("2 total listings: 1 scored, 1 unscored", res["summary"])

    def test_skill_demand(self) -> None:
        _seed_verified_cycle(self.db_path)
        # Populate extraction cache
        storage.put_extraction(
            self.db_path, "h1",
            {"required_skills": ["python", "k8s"], "nice_to_have": ["docker"]},
        )
        storage.put_extraction(
            self.db_path, "h2",
            {"required_skills": ["python", "sql"], "nice_to_have": ["kubernetes"]},
        )

        # Query canonical alias "k8s" -> "kubernetes"
        res = skill_demand("k8s", config=self.cfg)
        self.assertEqual(len(res["rows"]), 1)
        row = res["rows"][0]
        self.assertEqual(row["skill"], "kubernetes")
        self.assertEqual(row["required_count"], 1)
        self.assertEqual(row["nice_to_have_count"], 1)
        self.assertEqual(row["total_mentions"], 2)
        self.assertEqual(row["market_percentage"], 100.0)
        self.assertIn("kubernetes", res["summary"])

        # Unknown skill returns empty rows + clean message without raising
        res_unknown = skill_demand("cobol", config=self.cfg)
        self.assertEqual(res_unknown["rows"], [])
        self.assertIn("was not found", res_unknown["summary"])


if __name__ == "__main__":
    unittest.main()
