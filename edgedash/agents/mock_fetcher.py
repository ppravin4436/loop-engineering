"""
MockFetcher — returns 12 realistic fake job listings without any network call.

4 of the 12 listings are STABLE: same source + url on every run, so their
computed id never changes. On the second run those 4 will be ignored by
INSERT OR IGNORE, making the dedup count directly observable.

The other 8 get a fresh timestamp in their URL on each run so they are always
treated as new (simulating a live feed).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from edgedash import storage
from edgedash.agents.base import Agent, AgentResult
from edgedash.config import Config

if TYPE_CHECKING:
    from edgedash.planning import StopConditions


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_listings(role: str, city: str, run_ts: str) -> list[dict]:
    """
    Build 12 mock listings parameterised by role and city.
    run_ts is embedded in the URLs of the 8 dynamic listings so each run
    looks like a fresh page to the dedup logic.
    """

    # ------------------------------------------------------------------
    # 4 STABLE listings  (url never changes → same id every run)
    # ------------------------------------------------------------------
    stable = [
        {
            "title": f"Senior {role}",
            "company": "Flipkart",
            "location": city,
            "url": "https://careers.flipkart.com/jobs/senior-data-analyst-blr",
            "source": "mock",
            "description": (
                "Own end-to-end analytics for the supply chain team. "
                "Must have: SQL, Python (Pandas, NumPy), Tableau. "
                "Nice to have: Spark, Airflow. 4+ years required."
            ),
            "posted_at": "2026-08-18",
        },
        {
            "title": f"Lead {role} – Growth",
            "company": "Swiggy",
            "location": city,
            "url": "https://swiggy.com/careers/lead-data-analyst-growth",
            "source": "mock",
            "description": (
                "Drive experimentation and A/B testing for growth features. "
                "Stack: Python, SQL, Mixpanel, Looker. "
                "5+ years in a product analytics role."
            ),
            "posted_at": "2026-08-17",
        },
        {
            "title": f"{role} – Finance",
            "company": "Razorpay",
            "location": city,
            "url": "https://razorpay.com/jobs/data-analyst-finance-blr",
            "source": "mock",
            "description": (
                "Build revenue and reconciliation dashboards for CFO office. "
                "Strong SQL essential. Python scripting a plus. "
                "Experience with Power BI or Metabase preferred."
            ),
            "posted_at": "2026-08-19",
        },
        {
            "title": f"Associate {role}",
            "company": "Meesho",
            "location": city,
            "url": "https://meesho.com/careers/associate-data-analyst",
            "source": "mock",
            "description": (
                "Support seller analytics and category performance reporting. "
                "Excel, SQL, and basic Python expected. "
                "1–2 years experience. Great growth path."
            ),
            "posted_at": "2026-08-20",
        },
    ]

    # ------------------------------------------------------------------
    # 8 DYNAMIC listings  (run_ts in URL → new id each run)
    # ------------------------------------------------------------------
    dynamic = [
        {
            "title": f"{role} – Marketing Science",
            "company": "PhonePe",
            "location": city,
            "url": f"https://phonepe.com/jobs/da-marketing-{run_ts}",
            "source": "mock",
            "description": (
                "Analyse campaign performance across paid and organic channels. "
                "Skills: SQL, Python, Google Analytics, BigQuery. "
                "3+ years in marketing analytics."
            ),
            "posted_at": "2026-08-20",
        },
        {
            "title": f"Staff {role}",
            "company": "CRED",
            "location": city,
            "url": f"https://cred.club/careers/staff-data-analyst-{run_ts}",
            "source": "mock",
            "description": (
                "Define metrics and own the data culture for a product vertical. "
                "Deep SQL, dbt, and Metabase experience expected. "
                "7+ years, people management a plus."
            ),
            "posted_at": "2026-08-19",
        },
        {
            "title": f"{role} – Risk",
            "company": "Navi Technologies",
            "location": city,
            "url": f"https://navi.com/jobs/risk-analyst-{run_ts}",
            "source": "mock",
            "description": (
                "Score loan applications and monitor portfolio health. "
                "Python (Scikit-learn), SQL, Excel required. "
                "Exposure to credit risk models a strong plus."
            ),
            "posted_at": "2026-08-18",
        },
        {
            "title": f"Remote {role} – SaaS",
            "company": "Chargebee",
            "location": "Remote",
            "url": f"https://chargebee.com/careers/remote-da-{run_ts}",
            "source": "mock",
            "description": (
                "Build self-serve analytics for subscription metrics (MRR, churn). "
                "Looker, SQL, Python. Fully remote, IST overlap required."
            ),
            "posted_at": "2026-08-20",
        },
        {
            "title": f"Business {role}",
            "company": "Ola Electric",
            "location": city,
            "url": f"https://olaelectric.com/jobs/bda-{run_ts}",
            "source": "mock",
            "description": (
                "Deliver weekly business reviews for manufacturing KPIs. "
                "Advanced Excel, SQL, Power BI mandatory. "
                "Python automation a nice-to-have."
            ),
            "posted_at": "2026-08-17",
        },
        {
            "title": f"{role} – Platform",
            "company": "Atlassian",
            "location": city,
            "url": f"https://atlassian.com/jobs/data-analyst-blr-{run_ts}",
            "source": "mock",
            "description": (
                "Instrument product telemetry and build Amplitude dashboards. "
                "SQL, Python, Amplitude, dbt. 2–4 years product analytics."
            ),
            "posted_at": "2026-08-16",
        },
        {
            "title": f"Junior {role}",
            "company": "upGrad",
            "location": city,
            "url": f"https://upgrad.com/careers/jr-analyst-{run_ts}",
            "source": "mock",
            "description": (
                "Support learner success team with cohort retention analysis. "
                "SQL and Excel required; Python and Tableau a plus. "
                "0–2 years experience welcome."
            ),
            "posted_at": "2026-08-20",
        },
        {
            "title": f"{role} – Data Platform",
            "company": "Freshworks",
            "location": city,
            "url": f"https://freshworks.com/jobs/data-platform-analyst-{run_ts}",
            "source": "mock",
            "description": (
                "Partner with engineering to improve data quality and pipelines. "
                "SQL, Python, Airflow, Spark. 3+ years in a data engineering "
                "adjacent role."
            ),
            "posted_at": "2026-08-19",
        },
    ]

    fetched = _now_utc()
    rows: list[dict] = []

    for item in stable + dynamic:
        listing_id = storage.make_listing_id(item["source"], item["url"])
        rows.append(
            {
                "id": listing_id,
                "title": item["title"],
                "company": item["company"],
                "location": item["location"],
                "url": item["url"],
                "description": item["description"],
                "source": item["source"],
                "posted_at": item["posted_at"],
                "fetched_at": fetched,
                "fit_score": None,
                "fit_reason": None,
            }
        )

    return rows


class MockFetcher:
    name: str = "mock_fetcher"

    def run(
        self,
        config: Config,
        db_path: str,
        stop: "StopConditions | None" = None,
    ) -> AgentResult:
        run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        rows = _build_listings(config.target_role, config.target_city, run_ts)

        new_count = storage.upsert_listings(db_path, rows)

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=new_count,
            notes=f"{len(rows)} listings attempted, {new_count} new (dedup filtered {len(rows) - new_count})",
        )
