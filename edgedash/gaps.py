"""
CLI command to view skill gap analysis snapshots and trend reports.

Usage:
    python -m edgedash.gaps           # Show latest snapshot
    python -m edgedash.gaps --trend   # Compare earliest vs latest snapshot
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from edgedash import storage
from edgedash.config import load_config

# Ensure UTF-8 output on Windows terminals without crashing on cp1252
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def print_gaps() -> None:
    """Print the latest skill gap snapshot in a readable terminal table."""
    config = load_config()
    storage.init_db(config.db_path)
    rows = storage.get_latest_gap_snapshot(config.db_path)

    if not rows:
        print("\nNo gap snapshot found in database. Run a cycle first (python run_cycle.py).\n")
        return

    computed_at = rows[0].get("computed_at", "unknown")
    run_id = rows[0].get("run_id", "unknown")
    max_cost = max((r["opportunity_cost"] for r in rows), default=1.0) or 1.0

    print()
    print("=" * 86)
    print(f"  EDGEDASH -- SKILL GAP REPORT  (Snapshot: {computed_at} | Run: {run_id})")
    print("=" * 86)
    header = f"{'RANK':<5} {'SKILL':<20} {'BLOCKED':<8} {'OPP COST':<9} {'MEAN FIT':<9} {'CONFIDENCE':<14} {'OPPORTUNITY BAR'}"
    print(header)
    print("-" * 86)

    for idx, r in enumerate(rows, 1):
        skill = r["skill"]
        blocked = r["listings_blocked"]
        opp_cost = r["opportunity_cost"]
        mean_score = r["mean_score"]
        conf_str = f"high ({blocked})" if blocked >= 3 else f"low ({blocked}) [!]"

        bar_len = int(round((opp_cost / max_cost) * 18))
        bar = "#" * max(1, bar_len) if opp_cost > 0 else ""

        print(
            f"{idx:<5} {skill:<20} {blocked:<8} {opp_cost:<9.2f} {mean_score:<9.1f} {conf_str:<14} {bar}"
        )

    print("-" * 86)
    print("  * Opportunity cost = sum(fit_score / 100) for blocked listings (rule 24).")
    print("  * [!] Low confidence indicates gaps computed from fewer than 3 listings (rule 27).\n")


def print_trend() -> None:
    """Compare earliest vs latest skill gap snapshots to report trends (rule 25)."""
    config = load_config()
    storage.init_db(config.db_path)
    runs = storage.get_gap_snapshot_runs(config.db_path)

    if not runs:
        print("\nNo gap snapshots found in database. Run a cycle first (python run_cycle.py).\n")
        return

    if len(runs) == 1:
        early_date = runs[0].get("computed_at", "unknown")
        print()
        print("=" * 86)
        print("  EDGEDASH -- SKILL GAP TREND REPORT")
        print("=" * 86)
        print(f"  Snapshot recorded : {early_date} (Run: {runs[0]['run_id']})")
        print("  Status            : Only 1 snapshot exists in the database.")
        print()
        print("  * Trend reporting requires at least 2 snapshots across different cycle runs.")
        print("  * Run more cycles over subsequent days to see meaningful trend analysis.")
        print("  * No interpolation or extrapolation is performed on a single data point.")
        print("-" * 86 + "\n")
        return

    earliest_run = runs[0]
    latest_run = runs[-1]
    earliest_rows = storage.get_gap_snapshot_by_run(config.db_path, earliest_run["run_id"])
    latest_rows = storage.get_gap_snapshot_by_run(config.db_path, latest_run["run_id"])

    t_early = earliest_run.get("computed_at", "unknown")
    t_late = latest_run.get("computed_at", "unknown")

    print()
    print("=" * 86)
    print("  EDGEDASH -- SKILL GAP TREND REPORT")
    print(f"  Window: {t_early}  -->  {t_late}  ({len(runs)} total runs)")
    print("=" * 86)
    print(f"{'RANK':<5} {'SKILL':<20} {'EARLIEST':<10} {'LATEST':<10} {'CHANGE':<11} {'CHANGE %':<11} {'STATUS'}")
    print("-" * 86)

    earliest_map = {r["skill"]: r for r in earliest_rows}
    latest_top10 = latest_rows[:10]
    earliest_top10_map = {r["skill"]: idx + 1 for idx, r in enumerate(earliest_rows[:10])}
    latest_top10_skills = {r["skill"] for r in latest_top10}

    for idx, r in enumerate(latest_top10, 1):
        skill = r["skill"]
        latest_cost = r["opportunity_cost"]

        if skill in earliest_map:
            early_cost = earliest_map[skill]["opportunity_cost"]
            diff = latest_cost - early_cost
            pct = ((diff / early_cost) * 100.0) if early_cost > 0 else 0.0
            diff_str = f"{diff:+.2f}"
            pct_str = f"{pct:+.1f}%"
            status = "UP" if diff > 0.01 else ("DOWN" if diff < -0.01 else "FLAT")
            early_str = f"{early_cost:.2f}"
        else:
            early_str = "0.00"
            diff_str = f"+{latest_cost:.2f}"
            pct_str = "NEW"
            status = "NEW"

        print(
            f"{idx:<5} {skill:<20} {early_str:<10} {latest_cost:<10.2f} {diff_str:<11} {pct_str:<11} {status}"
        )

    # Track skills that dropped out of the top 10
    dropped = [r for r in earliest_rows[:10] if r["skill"] not in latest_top10_skills]
    if dropped:
        print("-" * 86)
        print("  DROPPED OUT OF TOP 10 (present in earliest top 10, dropped in latest):")
        for r in dropped:
            s_name = r["skill"]
            old_rank = earliest_top10_map[s_name]
            old_cost = r["opportunity_cost"]
            curr_row = next((x for x in latest_rows if x["skill"] == s_name), None)
            if curr_row:
                curr_rank = next(i + 1 for i, x in enumerate(latest_rows) if x["skill"] == s_name)
                curr_note = f"now rank #{curr_rank} (cost {curr_row['opportunity_cost']:.2f})"
            else:
                curr_note = "no longer in ranked gaps"
            print(f"   * {s_name:<18} (was #{old_rank}, cost {old_cost:.2f} -> {curr_note})")

    print("-" * 86)
    print("  * Opportunity cost = sum(fit_score / 100) across blocked listings (rule 24).\n")


if __name__ == "__main__":
    if "--trend" in sys.argv:
        print_trend()
    else:
        print_gaps()
