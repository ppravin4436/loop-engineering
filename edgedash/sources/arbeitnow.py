"""
Arbeitnow job board source.

API docs: https://www.arbeitnow.com/api/job-board-api
- Free, no API key, no signup required.
- Returns ~175 results per page; we cap at MAX_PAGES.
- Rate-limit: 1 request/second (steering rule 14).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from edgedash.config import Config
from edgedash.sources.base import register
from edgedash.sources.http import SourceError, get_json


_API_BASE = "https://www.arbeitnow.com/api/job-board-api"
_MAX_PAGES = 5
_MIN_RESULTS_BEFORE_RELAX_LOCATION = 5
_REQUEST_INTERVAL = 1.0  # seconds between requests (steering rule 14)


def _unix_to_iso(ts: int | None) -> str | None:
    """Convert a Unix timestamp integer to an ISO-8601 date string, or None."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return None


def _matches_keywords(job: dict, keywords: list[str]) -> bool:
    """Return True if any keyword appears in the job title, description, or tags."""
    if not keywords:
        return True
    searchable = " ".join(
        [
            job.get("title", ""),
            job.get("description", ""),
            " ".join(job.get("tags", [])),
        ]
    ).lower()
    return any(kw.lower() in searchable for kw in keywords)


def _matches_location(job: dict, city: str) -> bool:
    """
    Return True if the job is in the target city, or is remote.
    Comparison is case-insensitive substring.
    """
    if job.get("remote", False):
        return True
    location = (job.get("location") or "").lower()
    return city.lower() in location


def _normalise(job: dict) -> dict:
    """Map an Arbeitnow API record onto our standard nine-key schema."""
    return {
        "source": "arbeitnow",
        "external_id": job.get("slug"),         # stable slug, not a hash
        "title": job.get("title") or None,
        "company": job.get("company_name") or None,
        "location": job.get("location") or None,
        "url": job.get("url") or None,
        "description": job.get("description") or None,
        "posted_at": _unix_to_iso(job.get("created_at")),
        "raw": job,
    }


@register
class ArbeitnowSource:
    name: str = "arbeitnow"

    def fetch(self, config: Config) -> list[dict]:
        """
        Fetch up to MAX_PAGES of results from Arbeitnow.

        Paging continues while:
          - there are more pages, AND
          - the current page contains at least one keyword match, AND
          - we have not reached MAX_PAGES.

        Filtering:
          1. Keyword filter applied first (title + description + tags).
          2. Location filter applied second (city substring or remote flag).
          3. If location filter would leave fewer than MIN_RESULTS_BEFORE_RELAX_LOCATION
             results, it is dropped and a log line is printed (steering rule 13-style
             transparency without crashing the cycle).
        """
        all_raw: list[dict] = []
        page = 1

        while page <= _MAX_PAGES:
            if page > 1:
                time.sleep(_REQUEST_INTERVAL)

            data = get_json(_API_BASE, params={"page": page})
            jobs: list[dict] = data.get("data", [])

            if not jobs:
                break

            # Keep paging only while this page has keyword-relevant results
            keyword_matches = [j for j in jobs if _matches_keywords(j, config.keywords)]
            all_raw.extend(keyword_matches)

            if not keyword_matches:
                # No relevant jobs on this page — stop paging
                break

            # Check whether there is a next page
            next_url = (data.get("links") or {}).get("next")
            if not next_url:
                break

            page += 1

        print(
            f"  [arbeitnow] {len(all_raw)} raw results after keyword filter "
            f"(fetched {page} page(s), cap={_MAX_PAGES})"
        )

        # Apply location filter
        location_filtered = [
            j for j in all_raw if _matches_location(j, config.target_city)
        ]

        if len(location_filtered) < _MIN_RESULTS_BEFORE_RELAX_LOCATION:
            print(
                f"  [arbeitnow] Location filter for '{config.target_city}' would keep "
                f"only {len(location_filtered)} result(s) — relaxing to include all "
                f"remote/nearby roles ({len(all_raw)} total)."
            )
            final_jobs = all_raw
        else:
            final_jobs = location_filtered

        print(
            f"  [arbeitnow] {len(final_jobs)} listings after final filter "
            f"(location filter {'relaxed' if final_jobs is all_raw else 'applied'})."
        )

        return [_normalise(j) for j in final_jobs]
