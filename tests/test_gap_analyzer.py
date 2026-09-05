"""
Unit tests for GapAnalyzer and opportunity cost arithmetic in edgedash.agents.gap_analyzer.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

# Ensure repo root is on sys.path when running directly
REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edgedash import storage
from edgedash.agents.gap_analyzer import compute_gaps, GapAnalyzer
from edgedash.config import Config

ALIASES = {
    "k8s": "kubernetes",
    "postgresql": "postgres",
    "psql": "postgres",
    "google cloud": "gcp",
}


class TestGapAnalyzer(unittest.TestCase):

    def test_opportunity_cost_ranking(self):
        """
        Verify opportunity cost weighting per rule 24:
        Listing 1 (score 85) requires Kubernetes -> adds 0.85
        Listing 2 (score 20) requires Rust -> adds 0.20
        Listing 3 (score 90) requires Kubernetes -> adds 0.90
        Total Kubernetes cost: 1.75. Total Rust cost: 0.20.
        Kubernetes must rank higher than Rust.
        """
        listings = [
            {
                "id": "job-1",
                "fit_score": 85,
                "facts": {"required_skills": ["Python", "k8s"], "nice_to_have": []},
            },
            {
                "id": "job-2",
                "fit_score": 20,
                "facts": {"required_skills": ["Rust"], "nice_to_have": []},
            },
            {
                "id": "job-3",
                "fit_score": 90,
                "facts": {"required_skills": ["Kubernetes (EKS)", "SQL"], "nice_to_have": []},
            },
        ]
        user_skills = ["Python", "SQL"]

        gaps = compute_gaps(listings, user_skills, ALIASES)

        self.assertEqual(len(gaps), 2)
        # 1st rank: kubernetes
        self.assertEqual(gaps[0]["skill"], "kubernetes")
        self.assertEqual(gaps[0]["listings_blocked"], 2)
        self.assertEqual(gaps[0]["opportunity_cost"], 1.75)
        self.assertEqual(gaps[0]["mean_score"], 87.5)
        self.assertEqual(gaps[0]["top_score"], 90)
        self.assertEqual(gaps[0]["example_ids"], ["job-3", "job-1"])

        # 2nd rank: rust
        self.assertEqual(gaps[1]["skill"], "rust")
        self.assertEqual(gaps[1]["listings_blocked"], 1)
        self.assertEqual(gaps[1]["opportunity_cost"], 0.20)
        self.assertEqual(gaps[1]["mean_score"], 20.0)

    def test_nice_to_have_separation(self):
        """Nice-to-have skills are tracked separately and not added to opportunity cost."""
        listings = [
            {
                "id": "job-1",
                "fit_score": 80,
                "facts": {
                    "required_skills": ["Python"],
                    "nice_to_have": ["Docker", "Airflow"],
                },
            },
            {
                "id": "job-2",
                "fit_score": 70,
                "facts": {
                    "required_skills": ["Python", "Docker"],
                    "nice_to_have": ["Airflow"],
                },
            },
        ]
        user_skills = ["Python"]

        gaps = compute_gaps(listings, user_skills, ALIASES)
        gap_map = {g["skill"]: g for g in gaps}

        # Docker is required in job-2, nice-to-have in job-1
        self.assertIn("docker", gap_map)
        self.assertEqual(gap_map["docker"]["listings_blocked"], 1)
        self.assertEqual(gap_map["docker"]["opportunity_cost"], 0.70)
        self.assertEqual(gap_map["docker"]["also_nice_to_have"], 1)

        # Airflow is only nice-to-have in both, never required -> not in gap_map
        self.assertNotIn("airflow", gap_map)

    def test_confidence_flag(self):
        """Gaps with < 3 listings are flagged as low confidence per rule 27."""
        listings = [
            {
                "id": f"job-{i}",
                "fit_score": 75,
                "facts": {"required_skills": ["Spark"], "nice_to_have": []},
            }
            for i in range(3)
        ]
        listings.append({
            "id": "job-solo",
            "fit_score": 80,
            "facts": {"required_skills": ["Snowflake"], "nice_to_have": []},
        })
        user_skills = ["Python"]

        gaps = compute_gaps(listings, user_skills, ALIASES)
        gap_map = {g["skill"]: g for g in gaps}

        self.assertEqual(gap_map["spark"]["confidence"], "high")
        self.assertEqual(gap_map["spark"]["listings_blocked"], 3)
        self.assertEqual(gap_map["snowflake"]["confidence"], "low")
        self.assertEqual(gap_map["snowflake"]["listings_blocked"], 1)

    def test_agent_run_and_snapshot_persistence(self):
        """Verify GapAnalyzer agent runs, writes snapshots, and never overwrites previous runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = str(Path(tmpdir) / "test.db")
            storage.init_db(db_file)

            cfg = Config(
                target_role="Data Analyst",
                target_city="Bengaluru",
                keywords=[],
                my_skills=["Python"],
                experience_years=3,
                db_path=db_file,
                min_fit_score=60,
                sources=[],
                use_mock_fetcher=False,
                llm_provider="gemini",
                llm_model="gemini-3.6-flash",
                score_batch_size=25,
                skill_aliases=ALIASES,
            )

            # Insert 2 scored listings
            l1 = {
                "id": "j1", "title": "Dev 1", "company": "A", "location": "Remote",
                "url": "http://1", "description": "Needs Python and K8s", "source": "test",
                "posted_at": None, "fetched_at": "2026-08-27T00:00:00Z", "fit_score": 80, "fit_reason": "ok"
            }
            l2 = {
                "id": "j2", "title": "Dev 2", "company": "B", "location": "Remote",
                "url": "http://2", "description": "Needs Python and Spark", "source": "test",
                "posted_at": None, "fetched_at": "2026-08-27T00:00:00Z", "fit_score": 90, "fit_reason": "ok"
            }
            storage.upsert_listings(db_file, [l1, l2])

            # Extraction cache
            h1 = storage.hashlib.sha256(l1["description"].encode("utf-8")).hexdigest()
            h2 = storage.hashlib.sha256(l2["description"].encode("utf-8")).hexdigest()
            storage.put_extraction(db_file, h1, {"required_skills": ["python", "k8s"], "nice_to_have": []})
            storage.put_extraction(db_file, h2, {"required_skills": ["python", "spark"], "nice_to_have": []})

            analyzer = GapAnalyzer()

            # Run 1
            res1 = analyzer.run(cfg, db_file)
            self.assertEqual(res1.status, "ok")
            self.assertEqual(res1.records_touched, 2)
            self.assertIn("top: spark", res1.notes)

            # Run 2 (Simulate second cycle run)
            res2 = analyzer.run(cfg, db_file)
            self.assertEqual(res2.status, "ok")

            # Check snapshot rows: both snapshots exist (4 total rows)
            with storage._connect(db_file) as conn:
                count = conn.execute("SELECT COUNT(*) FROM skill_gaps").fetchone()[0]
                self.assertEqual(count, 4)

            latest = storage.get_latest_gap_snapshot(db_file)
            self.assertEqual(len(latest), 2)
            self.assertEqual(latest[0]["skill"], "spark")
            self.assertEqual(latest[0]["opportunity_cost"], 0.90)

    def test_trend_reporting(self):
        """Test multi-snapshot comparison: absolute change, %, NEW skills, and DROPPED skills."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = str(Path(tmpdir) / "test_trend.db")
            storage.init_db(db_file)

            # Snapshot 1: earliest
            run1 = "run_1"
            t1 = "2026-08-20T10:00:00"
            rows1 = [
                {"skill": "kubernetes", "listings_blocked": 5, "opportunity_cost": 4.0, "mean_score": 80.0, "example_ids": ["1"], "also_nice_to_have": 0},
                {"skill": "docker", "listings_blocked": 3, "opportunity_cost": 2.5, "mean_score": 83.3, "example_ids": ["2"], "also_nice_to_have": 0},
                {"skill": "spark", "listings_blocked": 2, "opportunity_cost": 1.8, "mean_score": 90.0, "example_ids": ["3"], "also_nice_to_have": 0},
            ]
            storage.save_gap_snapshot(db_file, run1, t1, rows1)

            # Snapshot 2: latest
            run2 = "run_2"
            t2 = "2026-08-27T10:00:00"
            rows2 = [
                {"skill": "kubernetes", "listings_blocked": 6, "opportunity_cost": 5.0, "mean_score": 83.3, "example_ids": ["1", "4"], "also_nice_to_have": 0},
                {"skill": "spark", "listings_blocked": 1, "opportunity_cost": 0.9, "mean_score": 90.0, "example_ids": ["3"], "also_nice_to_have": 0},
                {"skill": "airflow", "listings_blocked": 2, "opportunity_cost": 1.6, "mean_score": 80.0, "example_ids": ["5"], "also_nice_to_have": 0},
            ]
            storage.save_gap_snapshot(db_file, run2, t2, rows2)

            runs = storage.get_gap_snapshot_runs(db_file)
            self.assertEqual(len(runs), 2)
            self.assertEqual(runs[0]["run_id"], "run_1")
            self.assertEqual(runs[1]["run_id"], "run_2")

            early = storage.get_gap_snapshot_by_run(db_file, "run_1")
            late = storage.get_gap_snapshot_by_run(db_file, "run_2")

            early_map = {r["skill"]: r for r in early}
            late_map = {r["skill"]: r for r in late}

            # kubernetes: 4.0 -> 5.0 (+1.0, +25.0%)
            self.assertEqual(late_map["kubernetes"]["opportunity_cost"] - early_map["kubernetes"]["opportunity_cost"], 1.0)

            # airflow: NEW in late_map
            self.assertNotIn("airflow", early_map)
            self.assertIn("airflow", late_map)

            # docker: dropped out from late top 3
            late_top2_skills = {r["skill"] for r in late[:2]}
            self.assertNotIn("docker", late_top2_skills)


if __name__ == "__main__":
    unittest.main()

