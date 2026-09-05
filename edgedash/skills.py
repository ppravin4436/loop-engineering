"""
Deterministic skill canonicalisation and auditing.

Pure functions only. No model calls, no network (steering rule 22 & 23).
All alias resolution and raw extraction auditing live here.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from typing import Mapping

from edgedash import storage
from edgedash.config import Config, load_config

# Ensure UTF-8 output on Windows terminals without crashing on cp1252
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_PAREN_RE = re.compile(r"\(.*?\)|\[.*?\]")
_WHITESPACE_RE = re.compile(r"\s+")
_STRIP_CHARS = " \t\n\r\"'“”‘’`,;:!?*~•_-"


def canonical(raw: str, aliases: Mapping[str, str] | None = None) -> str:
    """
    Canonicalise a skill name deterministically.

    1. Drop parenthetical qualifiers (e.g. "kubernetes (eks)" -> "kubernetes")
    2. Convert to lowercase
    3. Strip surrounding whitespace and punctuation
    4. Collapse internal whitespace
    5. Lookup in alias map (if provided)
    """
    if not raw or not isinstance(raw, str):
        return ""

    text = _PAREN_RE.sub(" ", raw)
    text = text.lower()
    text = text.strip(_STRIP_CHARS)
    if text.endswith(".") and not text.startswith("."):
        text = text.rstrip(".")
    text = _WHITESPACE_RE.sub(" ", text).strip()

    if not text:
        return ""

    if aliases:
        return aliases.get(text, text)
    return text


def audit_skills(config: Config) -> None:
    """
    Read-only audit of all extracted required_skills in the database.
    Prints top 40 raw skills with canonical mappings and singleton raw strings.
    """
    storage.init_db(config.db_path)
    extracted_batches = storage.get_all_extracted_skills(config.db_path)
    aliases = config.skill_aliases

    all_raw: list[str] = [
        skill.strip()
        for batch in extracted_batches
        for skill in batch
        if skill and isinstance(skill, str) and skill.strip()
    ]

    if not all_raw:
        print("\nNo extracted skills found in database. Run a fetch & score cycle first.\n")
        return

    counts = Counter(all_raw)
    total_occurrences = len(all_raw)
    unique_count = len(counts)

    print("=" * 72)
    print(f"  SKILL AUDIT — {total_occurrences} total skill mentions, {unique_count} unique raw strings")
    print("=" * 72)
    print(f"{'RANK':<5} {'COUNT':<7} {'RAW STRING':<28} -> {'CANONICAL FORM'}")
    print("-" * 72)

    most_common = counts.most_common(40)
    for idx, (raw_skill, count) in enumerate(most_common, 1):
        canon = canonical(raw_skill, aliases)
        arrow = "==" if canon == raw_skill.lower() else "->"
        print(f"{idx:<5} {count:<7} {raw_skill:<28} {arrow} {canon}")

    singletons = sorted([s for s, c in counts.items() if c == 1])

    print("\n" + "=" * 72)
    print(f"  SINGLETONS ({len(singletons)} strings seen exactly once — review for aliases/typos)")
    print("=" * 72)
    if singletons:
        for s in singletons:
            canon = canonical(s, aliases)
            mapping_note = f" (maps to '{canon}')" if canon != s.lower() else ""
            print(f"  • {s}{mapping_note}")
    else:
        print("  (None)")
    print("-" * 72)


if __name__ == "__main__":
    if "--audit" in sys.argv:
        cfg = load_config()
        audit_skills(cfg)
    else:
        print("Usage: python -m edgedash.skills --audit")
        sys.exit(1)
