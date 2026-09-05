"""
Unit tests for skill canonicalisation logic in edgedash.skills.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

# Ensure repo root is on sys.path when running directly
REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edgedash.skills import canonical

ALIASES: dict[str, str] = {
    "k8s": "kubernetes",
    "js": "javascript",
    "nodejs": "node",
    "node.js": "node",
    "postgresql": "postgres",
    "psql": "postgres",
    "gcp": "gcp",
    "google cloud": "gcp",
    "google cloud platform": "gcp",
    "ml": "machine learning",
    "ci/cd": "ci/cd",
    "ci cd": "ci/cd",
    "cicd": "ci/cd",
}


class TestCanonical(unittest.TestCase):

    def test_case_folding(self):
        """Case is normalized to lowercase."""
        self.assertEqual(canonical("PYTHON", ALIASES), "python")
        self.assertEqual(canonical("Docker", ALIASES), "docker")
        self.assertEqual(canonical("PostgreSQL", ALIASES), "postgres")

    def test_whitespace_handling(self):
        """Surrounding and internal whitespace is collapsed and stripped."""
        self.assertEqual(canonical("   python   ", ALIASES), "python")
        self.assertEqual(canonical("machine    learning", ALIASES), "machine learning")
        self.assertEqual(canonical("\tgoogle   cloud  \n", ALIASES), "gcp")

    def test_parentheses_dropped(self):
        """Parenthetical qualifiers are dropped."""
        self.assertEqual(canonical("kubernetes (eks)", ALIASES), "kubernetes")
        self.assertEqual(canonical("AWS (Amazon Web Services)", ALIASES), "aws")
        self.assertEqual(canonical("python (3.11+)", ALIASES), "python")
        self.assertEqual(canonical("SQL [Postgres/MySQL]", ALIASES), "sql")

    def test_aliased_term(self):
        """Aliased terms correctly map to canonical form."""
        self.assertEqual(canonical("k8s", ALIASES), "kubernetes")
        self.assertEqual(canonical("psql", ALIASES), "postgres")
        self.assertEqual(canonical("google cloud platform", ALIASES), "gcp")
        self.assertEqual(canonical("ml", ALIASES), "machine learning")
        self.assertEqual(canonical("ci cd", ALIASES), "ci/cd")
        self.assertEqual(canonical("cicd", ALIASES), "ci/cd")

    def test_unaliased_term(self):
        """Terms without an alias retain their clean string form."""
        self.assertEqual(canonical("pandas", ALIASES), "pandas")
        self.assertEqual(canonical("fastapi", ALIASES), "fastapi")
        self.assertEqual(canonical("snowflake", ALIASES), "snowflake")

    def test_empty_and_whitespace_only(self):
        """Empty string or whitespace returns empty string."""
        self.assertEqual(canonical("", ALIASES), "")
        self.assertEqual(canonical("   ", ALIASES), "")
        self.assertEqual(canonical("  \n\t  ", ALIASES), "")

    def test_node_vs_javascript_separation(self):
        """node / nodejs / node.js remain distinct from js / javascript."""
        self.assertEqual(canonical("node", ALIASES), "node")
        self.assertEqual(canonical("nodejs", ALIASES), "node")
        self.assertEqual(canonical("node.js", ALIASES), "node")
        self.assertEqual(canonical("js", ALIASES), "javascript")
        self.assertEqual(canonical("javascript", ALIASES), "javascript")
        self.assertNotEqual(canonical("node.js", ALIASES), canonical("javascript", ALIASES))


if __name__ == "__main__":
    unittest.main()
