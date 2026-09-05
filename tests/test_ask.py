"""
Unit tests for the two-call query pipeline in edgedash/query/ask.py (rules 40–46).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edgedash import storage
from edgedash.config import Config
from edgedash.query.ask import Answer, ask


def _make_cfg(db_path: str) -> Config:
    return Config(
        db_path=db_path,
        skill_aliases={},
    )


def _seed_verified_cycle(db_path: str) -> None:
    storage.log_cycle(
        db_path=db_path,
        agent="cycle_summary",
        started_at="2026-09-01T10:00:00+00:00",
        finished_at="2026-09-01T10:05:00+00:00",
        records_touched=5,
        status="ok",
        notes="verdict=ok",
    )


class TestAskPipeline(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test_ask.db")
        storage.init_db(self.db_path)
        _seed_verified_cycle(self.db_path)
        self.cfg = _make_cfg(self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_empty_question_returns_cleanly(self) -> None:
        ans = ask("", config=self.cfg)
        self.assertFalse(ans.answerable)
        self.assertIn("Please enter a question", ans.text)
        self.assertEqual(ans.rows, [])

    @patch("edgedash.llm.complete_json")
    def test_unmatched_tool_returns_listing_of_tools_without_phrasing_call(self, mock_llm) -> None:
        # Route returns tool: null
        mock_llm.return_value = {
            "tool": None,
            "params": {},
            "confidence": "low",
        }

        ans = ask("What is the weather in Paris?", config=self.cfg)
        # Should only call LLM once (for route), not for phrase
        self.assertEqual(mock_llm.call_count, 1)
        self.assertFalse(ans.answerable)
        self.assertIsNone(ans.tool_used)
        self.assertIn("I cannot answer this question", ans.text)
        self.assertIn("companies_hiring", ans.text)
        self.assertIn("best_matches", ans.text)

        # Check query_log recorded as unanswerable
        logs = storage.get_recent_queries(self.db_path)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["answerable"], 0)
        self.assertIsNone(logs[0]["tool_chosen"])

    @patch("edgedash.llm.complete_json")
    def test_invalid_tool_name_handled_as_unanswerable(self, mock_llm) -> None:
        # Route returns an invented tool
        mock_llm.return_value = {
            "tool": "invented_sql_generator",
            "params": {},
            "confidence": "low",
        }

        ans = ask("Generate a custom table", config=self.cfg)
        self.assertEqual(mock_llm.call_count, 1)
        self.assertFalse(ans.answerable)
        self.assertIsNone(ans.tool_used)
        self.assertIn("I cannot answer this question", ans.text)

    @patch("edgedash.llm.complete_json")
    def test_successful_two_call_flow(self, mock_llm) -> None:
        # Insert test listing
        storage.upsert_listings(
            self.db_path,
            [
                {
                    "id": "l1", "title": "Data Analyst", "company": "Acme", "location": "Remote",
                    "url": "http://example.com/1", "description": "desc", "source": "test",
                    "posted_at": "2026-09-01T00:00:00+00:00", "fetched_at": "2026-09-01T12:00:00+00:00",
                    "fit_score": 85, "fit_reason": "matches",
                }
            ],
        )

        # Call 1: Route
        # Call 2: Phrase
        mock_llm.side_effect = [
            {"tool": "best_matches", "params": {"n": 5}, "confidence": "high"},
            {"answer": "Your top recommendation is Data Analyst at Acme with an 85 fit score."},
        ]

        ans = ask("What are my top matching jobs?", config=self.cfg)
        self.assertEqual(mock_llm.call_count, 2)
        self.assertTrue(ans.answerable)
        self.assertEqual(ans.tool_used, "best_matches")
        self.assertEqual(len(ans.rows), 1)
        self.assertEqual(ans.rows[0]["company"], "Acme")
        self.assertIn("Acme", ans.text)

        # Verify query_log record
        logs = storage.get_recent_queries(self.db_path)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["answerable"], 1)
        self.assertEqual(logs[0]["tool_chosen"], "best_matches")

    @patch("edgedash.llm.complete_json")
    def test_empty_rows_phrasing_handled(self, mock_llm) -> None:
        # Route succeeds but database has 0 matching listings
        mock_llm.return_value = {
            "tool": "companies_hiring",
            "params": {"days": 7},
            "confidence": "high",
        }

        ans = ask("Which companies are hiring right now?", config=self.cfg)
        self.assertTrue(ans.answerable)
        self.assertEqual(ans.rows, [])
        self.assertIn("0 rows", ans.text)


if __name__ == "__main__":
    unittest.main()
