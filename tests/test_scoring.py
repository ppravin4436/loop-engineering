"""
Unit tests for deterministic scoring logic in edgedash.scoring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

# Ensure repo root is on sys.path when running directly
REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edgedash.config import Config
from edgedash.scoring import score_listing, build_reason, SENIORITY_BANDS


def _make_config(
    my_skills: list[str] | None = None,
    target_seniority: str = "mid",
    target_city: str = "Bengaluru",
    score_weights: dict[str, float] | None = None,
) -> Config:
    return Config(
        target_role="Data Analyst",
        target_city=target_city,
        keywords=["Python", "SQL"],
        my_skills=my_skills if my_skills is not None else ["Python", "SQL", "Pandas"],
        experience_years=3,
        db_path="test.db",
        min_fit_score=60,
        sources=["arbeitnow"],
        use_mock_fetcher=False,
        llm_provider="gemini",
        llm_model="gemini-3.6-flash",
        score_batch_size=25,
        target_seniority=target_seniority,
        score_weights=score_weights or {
            "skill_match": 0.45,
            "seniority_fit": 0.25,
            "location_fit": 0.15,
            "recency": 0.15,
        },
    )


class TestScoring(unittest.TestCase):

    def test_perfect_match(self):
        """All skills match, seniority exact, remote, posted today -> 100 score."""
        cfg = _make_config(my_skills=["python", "sql", "pandas", "docker"])
        listing = {
            "title": "Data Analyst",
            "company": "Acme Corp",
            "location": "Remote",
            "posted_at": datetime.now(timezone.utc).isoformat(),
        }
        facts = {
            "required_skills": ["Python", "SQL"],
            "nice_to_have": ["Pandas"],
            "seniority": "mid",
            "years_required": 3,
            "remote_ok": True,
        }
        res = score_listing(listing, facts, cfg)
        self.assertEqual(res["score"], 100)
        self.assertEqual(res["components"]["skill_match"], 1.0)
        self.assertEqual(res["components"]["seniority_fit"], 1.0)
        self.assertEqual(res["components"]["location_fit"], 1.0)
        self.assertEqual(res["components"]["recency"], 1.0)
        self.assertIn("2/2 required skills", res["reason"])
        self.assertIn("seniority fits", res["reason"])
        self.assertIn("remote", res["reason"])
        self.assertIn("no skill gaps", res["reason"])

    def test_zero_match(self):
        """No skills match, seniority 3 bands off, wrong location, old posting -> 2 score (0.1 location * 0.15 weight)."""
        cfg = _make_config(my_skills=["python"], target_seniority="junior", target_city="Bengaluru")
        listing = {
            "title": "Lead Architect",
            "company": "Big Corp",
            "location": "Berlin",
            "posted_at": "2020-01-01T00:00:00Z",
        }
        facts = {
            "required_skills": ["Rust", "C++", "Kubernetes"],
            "nice_to_have": ["Haskell"],
            "seniority": "lead",
            "years_required": 10,
            "remote_ok": False,
        }
        res = score_listing(listing, facts, cfg)
        # skill: 0.0, seniority: 0.0, location: 0.1 (not remote, not city), recency: 0.0 -> 0.1 * 0.15 = 0.015 -> 2
        self.assertLessEqual(res["score"], 5)
        self.assertEqual(res["components"]["skill_match"], 0.0)
        self.assertEqual(res["components"]["seniority_fit"], 0.0)
        self.assertEqual(res["components"]["location_fit"], 0.1)
        self.assertEqual(res["components"]["recency"], 0.0)
        self.assertIn("gap: Rust, C++, Kubernetes", res["reason"])
        self.assertIn("seniority mismatch", res["reason"])

    def test_empty_required_skills(self):
        """No required skills listed -> does not crash and handles zero division."""
        cfg = _make_config(my_skills=["python"])
        listing = {"title": "Analyst", "location": "Remote"}
        facts = {
            "required_skills": [],
            "nice_to_have": [],
            "seniority": "mid",
            "years_required": None,
            "remote_ok": True,
        }
        res = score_listing(listing, facts, cfg)
        self.assertEqual(res["components"]["skill_match"], 1.0)
        self.assertIn("no skills specified", res["reason"])

    def test_null_posted_at(self):
        """Null posted_at defaults recency to 0.5 without crashing."""
        cfg = _make_config()
        listing = {"title": "Analyst", "location": "Remote", "posted_at": None}
        facts = {
            "required_skills": ["Python"],
            "nice_to_have": [],
            "seniority": "mid",
            "years_required": None,
            "remote_ok": True,
        }
        res = score_listing(listing, facts, cfg)
        self.assertEqual(res["components"]["recency"], 0.5)
        self.assertIn("posted date unknown", res["reason"])

    def test_null_remote_ok(self):
        """Null remote_ok and unknown location defaults location_fit to 0.5."""
        cfg = _make_config(target_city="Bengaluru")
        listing = {"title": "Analyst", "location": "Unknown", "posted_at": None}
        facts = {
            "required_skills": ["Python"],
            "nice_to_have": [],
            "seniority": "mid",
            "years_required": None,
            "remote_ok": None,
        }
        res = score_listing(listing, facts, cfg)
        self.assertEqual(res["components"]["location_fit"], 0.5)
        self.assertIn("location unknown", res["reason"])

    def test_seniority_three_bands_off(self):
        """Junior vs Lead is 3 bands away -> seniority_fit is 0.0."""
        cfg = _make_config(target_seniority="junior")
        listing = {"title": "Lead Data Lead", "location": "Remote"}
        facts = {
            "required_skills": ["Python"],
            "nice_to_have": [],
            "seniority": "lead",
            "years_required": 8,
            "remote_ok": True,
        }
        res = score_listing(listing, facts, cfg)
        self.assertEqual(res["components"]["seniority_fit"], 0.0)
        self.assertIn("seniority mismatch", res["reason"])

    def test_gap_formatting_in_reason(self):
        """Missing skills are explicitly named in the reason string."""
        cfg = _make_config(my_skills=["Python", "SQL"])
        listing = {"title": "Engineer", "location": "Remote"}
        facts = {
            "required_skills": ["Python", "SQL", "Kubernetes", "Spark"],
            "nice_to_have": [],
            "seniority": "mid",
            "years_required": 3,
            "remote_ok": True,
        }
        res = score_listing(listing, facts, cfg)
        self.assertIn("2/4 required skills", res["reason"])
        self.assertIn("gap: Kubernetes, Spark", res["reason"])


if __name__ == "__main__":
    unittest.main()
