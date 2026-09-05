"""
Verification checks (steering rules 34–39).

All checks are pure functions. No clock, no network, no database reads.
`now` is always a parameter — never datetime.now() inside a check.

Every check returns a CheckResult. run_all_checks() collects them into a
Verdict. The Verifier agent calls run_all_checks(); it never repairs data.

Rule 35 reminder: these checks assert plausibility of the output
*distribution*, not the correctness of any single value.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    observed: str    # human-readable observed value, e.g. "spread=3"
    threshold: str   # human-readable threshold, e.g. "min=10"
    message: str     # full sentence suitable for logging (rule 37)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    failed_checks: list[CheckResult]
    all_checks: list[CheckResult]

    @property
    def summary(self) -> str:
        if self.passed:
            return f"PASS ({len(self.all_checks)} checks)"
        names = ", ".join(c.name for c in self.failed_checks)
        return f"FAIL — checks failed: {names}"


# ---------------------------------------------------------------------------
# Check 1: score spread
# ---------------------------------------------------------------------------

def check_score_spread(scores: list[int], config: Config) -> CheckResult:
    """
    FAILS if max-min < min_score_spread OR stdev < min_score_stdev.

    Catches the inflation failure mode: a scorer that pushes all scores
    into a narrow band is not discriminating between good and poor matches.

    Trivially passes (with an informative message) when fewer than 5 scores
    are present — too few data points to make a spread judgment.
    """
    name = "score_spread"
    spread_threshold = config.min_score_spread
    stdev_threshold  = config.min_score_stdev

    if len(scores) < 5:
        return CheckResult(
            name=name,
            passed=True,
            observed=f"n={len(scores)}",
            threshold=f"min_n=5",
            message=(
                f"{name}: trivially passed — only {len(scores)} score(s), "
                f"need at least 5 to evaluate spread."
            ),
        )

    spread = max(scores) - min(scores)
    stdev  = statistics.stdev(scores)  # sample stdev; n>=5 so this is safe

    # Both conditions must hold for a pass
    spread_ok = spread >= spread_threshold
    stdev_ok  = stdev  >= stdev_threshold

    if spread_ok and stdev_ok:
        return CheckResult(
            name=name,
            passed=True,
            observed=f"spread={spread}, stdev={stdev:.1f}",
            threshold=f"min_spread={spread_threshold}, min_stdev={stdev_threshold}",
            message=(
                f"{name}: passed — spread={spread} >= {spread_threshold}, "
                f"stdev={stdev:.1f} >= {stdev_threshold}."
            ),
        )

    # Build a precise failure message naming the observed value (rule 37)
    failures: list[str] = []
    if not spread_ok:
        failures.append(
            f"spread={spread} < min_score_spread={spread_threshold}"
        )
    if not stdev_ok:
        failures.append(
            f"stdev={stdev:.1f} < min_score_stdev={stdev_threshold}"
        )

    return CheckResult(
        name=name,
        passed=False,
        observed=f"spread={spread}, stdev={stdev:.1f}",
        threshold=f"min_spread={spread_threshold}, min_stdev={stdev_threshold}",
        message=f"{name}: FAILED — {'; '.join(failures)}.",
    )


# ---------------------------------------------------------------------------
# Check 2: extraction sanity
# ---------------------------------------------------------------------------

def check_extraction_sanity(
    facts_list: list[dict[str, Any]],
    config: Config,
) -> CheckResult:
    """
    FAILS if:
      - More than max_empty_extraction_pct% of listings have an empty
        required_skills list (catches a broken extractor).
      - Any listing has more than max_skills_per_listing skills (catches
        a model that returned a sentence as a single skill entry).

    `facts_list` is a list of extraction fact dicts, each with a
    'required_skills' key (list of strings).
    """
    name = "extraction_sanity"
    empty_pct_threshold  = config.max_empty_extraction_pct
    max_skills_threshold = config.max_skills_per_listing
    thresh_pct_str = f"{int(empty_pct_threshold)}%" if float(empty_pct_threshold).is_integer() else f"{empty_pct_threshold}%"

    if not facts_list:
        return CheckResult(
            name=name,
            passed=True,
            observed="n=0",
            threshold=f"max_empty={thresh_pct_str}, max_skills={max_skills_threshold}",
            message=f"{name}: trivially passed — no extractions to check.",
        )

    n = len(facts_list)
    empty_count = sum(
        1 for f in facts_list
        if not (f.get("required_skills") or [])
    )
    empty_pct = (empty_count / n) * 100.0

    # Find the worst offender for the oversize-skills check
    max_observed = max(
        (len(f.get("required_skills") or []) for f in facts_list),
        default=0,
    )

    empty_ok     = empty_pct    <= empty_pct_threshold
    max_skills_ok = max_observed <= max_skills_threshold

    if empty_ok and max_skills_ok:
        return CheckResult(
            name=name,
            passed=True,
            observed=f"empty={empty_pct:.1f}%, max_skills={max_observed}",
            threshold=f"max_empty={thresh_pct_str}, max_skills={max_skills_threshold}",
            message=(
                f"{name}: passed — empty={empty_pct:.1f}% <= {thresh_pct_str}, "
                f"max_skills={max_observed} <= {max_skills_threshold}."
            ),
        )

    failures: list[str] = []
    if not empty_ok:
        failures.append(
            f"empty_pct={empty_pct:.1f}% > max_empty_extraction_pct={thresh_pct_str}"
        )
    if not max_skills_ok:
        failures.append(
            f"max_skills_per_listing={max_observed} > threshold={max_skills_threshold}"
        )

    return CheckResult(
        name=name,
        passed=False,
        observed=f"empty={empty_pct:.1f}%, max_skills={max_observed}",
        threshold=f"max_empty={thresh_pct_str}, max_skills={max_skills_threshold}",
        message=f"{name}: FAILED — {'; '.join(failures)}.",
    )


# ---------------------------------------------------------------------------
# Check 3: gap sample size
# ---------------------------------------------------------------------------

def check_gap_sample_size(
    gaps: list[dict[str, Any]],
    config: Config,
) -> CheckResult:
    """
    FAILS if the top-ranked gap was computed from fewer than min_gap_sample
    listings. Catches ranking a gap that was observed in only one or two
    listings — not enough evidence to act on.

    `gaps` is an ordered list of gap dicts, each with 'listings_blocked'.
    """
    name = "gap_sample_size"
    threshold = config.min_gap_sample

    if not gaps:
        return CheckResult(
            name=name,
            passed=True,
            observed="n=0 gaps",
            threshold=f"min_sample={threshold}",
            message=f"{name}: trivially passed — no gaps to check.",
        )

    top_gap    = gaps[0]
    top_skill  = top_gap.get("skill", "unknown")
    top_sample = top_gap.get("listings_blocked", 0)

    if top_sample >= threshold:
        return CheckResult(
            name=name,
            passed=True,
            observed=f"top_gap='{top_skill}' listings_blocked={top_sample}",
            threshold=f"min_gap_sample={threshold}",
            message=(
                f"{name}: passed — top gap '{top_skill}' has "
                f"{top_sample} listings >= min_gap_sample={threshold}."
            ),
        )

    return CheckResult(
        name=name,
        passed=False,
        observed=f"top_gap='{top_skill}' listings_blocked={top_sample}",
        threshold=f"min_gap_sample={threshold}",
        message=(
            f"{name}: FAILED — top gap '{top_skill}' computed from "
            f"listings_blocked={top_sample} < min_gap_sample={threshold}."
        ),
    )


# ---------------------------------------------------------------------------
# Check 4: data freshness
# ---------------------------------------------------------------------------

def check_freshness(
    latest_fetch_at: str | None,
    config: Config,
    now: datetime,
) -> CheckResult:
    """
    FAILS if the newest listing is older than max_data_age_days.
    Catches the case where a silent fetch failure left stale data.

    `now` is a parameter — never datetime.now() inside this function,
    so callers control the reference time and tests are deterministic.
    """
    name = "freshness"
    threshold_days = config.max_data_age_days

    if latest_fetch_at is None:
        return CheckResult(
            name=name,
            passed=False,
            observed="latest_fetch_at=None",
            threshold=f"max_data_age_days={threshold_days}",
            message=(
                f"{name}: FAILED — no listings have ever been fetched "
                f"(latest_fetch_at=None)."
            ),
        )

    try:
        dt = datetime.fromisoformat(latest_fetch_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"latest_fetch_at='{latest_fetch_at}' (unparseable)",
            threshold=f"max_data_age_days={threshold_days}",
            message=(
                f"{name}: FAILED — could not parse latest_fetch_at="
                f"'{latest_fetch_at}' (unparseable timestamp)."
            ),
        )

    age_days = (now - dt).total_seconds() / 86_400.0

    if age_days <= threshold_days:
        return CheckResult(
            name=name,
            passed=True,
            observed=f"age={age_days:.2f}d",
            threshold=f"max_data_age_days={threshold_days}",
            message=(
                f"{name}: passed — data age={age_days:.2f}d "
                f"<= max_data_age_days={threshold_days}."
            ),
        )

    return CheckResult(
        name=name,
        passed=False,
        observed=f"age={age_days:.2f}d",
        threshold=f"max_data_age_days={threshold_days}",
        message=(
            f"{name}: FAILED — data age={age_days:.2f}d "
            f"> max_data_age_days={threshold_days}."
        ),
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def run_all_checks(
    scores: list[int],
    facts_list: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    latest_fetch_at: str | None,
    config: Config,
    now: datetime,
) -> Verdict:
    """
    Run every verification check and return a Verdict.
    Passes only when all individual checks pass.
    """
    results: list[CheckResult] = [
        check_score_spread(scores, config),
        check_extraction_sanity(facts_list, config),
        check_gap_sample_size(gaps, config),
        check_freshness(latest_fetch_at, config, now),
    ]

    failed = [r for r in results if not r.passed]
    return Verdict(
        passed=len(failed) == 0,
        failed_checks=failed,
        all_checks=results,
    )
