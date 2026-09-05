"""
Centralised storage module. Rule 2: every read and write goes through here.

SWAP LINE: This is the ONLY module permitted to import a DB driver.

Two dialects, selected by the `db_path` / connection specifier:
  - A file path (default local dev/tests)            -> sqlite3
  - `postgres://` or `postgresql://` URL (production) -> psycopg2
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote as _url_quote, urlparse, urlunparse

try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    _HAS_PSYCOPG = True
except Exception:  # pragma: no cover
    _HAS_PSYCOPG = False


# ---------------------------------------------------------------------------
# Dialect helpers
# ---------------------------------------------------------------------------


def _is_pg(conn_spec: str) -> bool:
    s = (conn_spec or "").lower()
    return s.startswith("postgres://") or s.startswith("postgresql://")


def _q(sql: str, is_pg: bool) -> str:
    """Translate ? placeholders for Postgres (uses %s)."""
    if not is_pg:
        return sql
    return sql.replace("?", "%s")


def _row_scalar(row, is_pg: bool):
    if row is None:
        return None
    if is_pg:
        values = list(row.values())
        return values[0] if values else None
    return row[0]


def _rows_to_dicts(rows) -> list[dict[str, Any]]:
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["example_ids"] = json.loads(d["example_ids"])
        except Exception:
            pass
        results.append(d)
    return results


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS listings (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    company        TEXT NOT NULL,
    location       TEXT NOT NULL,
    url            TEXT NOT NULL,
    description    TEXT,
    source         TEXT NOT NULL,
    posted_at      TEXT,
    fetched_at     TEXT NOT NULL,
    fit_score      INTEGER,
    fit_reason     TEXT,
    fit_components TEXT,
    scored_at      TEXT
);

CREATE TABLE IF NOT EXISTS skill_gaps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    computed_at       TEXT NOT NULL,
    skill             TEXT NOT NULL,
    listings_blocked  INTEGER NOT NULL,
    opportunity_cost  REAL NOT NULL,
    mean_score        REAL NOT NULL,
    top_score         INTEGER,
    example_ids       TEXT NOT NULL,
    also_nice_to_have INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cycle_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    records_touched INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS extraction_cache (
    description_hash TEXT PRIMARY KEY,
    payload          TEXT NOT NULL,
    extracted_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT NOT NULL,
    tool_chosen TEXT,
    params      TEXT,
    answerable  INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    created_at  TEXT NOT NULL
);
"""

_PG_DDL = [
    """
    CREATE TABLE IF NOT EXISTS listings (
        id             TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        company        TEXT NOT NULL,
        location       TEXT NOT NULL,
        url            TEXT NOT NULL,
        description    TEXT,
        source         TEXT NOT NULL,
        posted_at      TEXT,
        fetched_at     TEXT NOT NULL,
        fit_score      INTEGER,
        fit_reason     TEXT,
        fit_components TEXT,
        scored_at      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_gaps (
        id                BIGSERIAL PRIMARY KEY,
        run_id            TEXT NOT NULL,
        computed_at       TEXT NOT NULL,
        skill             TEXT NOT NULL,
        listings_blocked  INTEGER NOT NULL,
        opportunity_cost  REAL NOT NULL,
        mean_score        REAL NOT NULL,
        top_score         INTEGER,
        example_ids       TEXT NOT NULL,
        also_nice_to_have INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cycle_log (
        id              BIGSERIAL PRIMARY KEY,
        agent           TEXT NOT NULL,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        records_touched INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL,
        notes           TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS extraction_cache (
        description_hash TEXT PRIMARY KEY,
        payload          TEXT NOT NULL,
        extracted_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS query_log (
        id          BIGSERIAL PRIMARY KEY,
        question    TEXT NOT NULL,
        tool_chosen TEXT,
        params      TEXT,
        answerable  INTEGER NOT NULL,
        duration_ms REAL NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
]


# ---------------------------------------------------------------------------
# Connection + cursor helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _connect(conn_spec: str):
    is_pg = _is_pg(conn_spec)
    if is_pg:
        if not _HAS_PSYCOPG:
            raise RuntimeError(
                "psycopg2 is not installed. Install psycopg2-binary to use Postgres."
            )
        dsn = _normalize_pg_url(conn_spec)
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as _cur:
                    _cur.execute("SET timezone = 'UTC'")
                yield conn
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(conn_spec)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def _normalize_pg_url(conn_spec: str) -> str:
    """Percent-encode the userinfo portion of a postgres:// URL so that
    passwords containing `@`, `:`, `[`, `]`, `%`, or spaces don't confuse
    the URL parser or psycopg2's DSN splitter.

    Strategy: split the scheme + // from the rest, then find the LAST `@`
    (everything before it is userinfo). urlparse can't help here because
    an unencoded `@` inside the password already splits the netloc wrongly.
    """
    if "://" not in conn_spec:
        return conn_spec
    scheme, _, remainder = conn_spec.partition("://")
    if scheme.lower() not in ("postgres", "postgresql"):
        return conn_spec
    if "@" not in remainder:
        return conn_spec
    userinfo, _, hostpart = remainder.rpartition("@")
    if not userinfo:
        return conn_spec
    # userinfo may be "user" or "user:password" or ":password"
    if ":" in userinfo:
        u, _, pw = userinfo.partition(":")
    else:
        u, pw = userinfo, None
    safe = ""
    u_enc = _url_quote(u, safe=safe) if u else ""
    if pw is None:
        userinfo_enc = u_enc
    else:
        userinfo_enc = f"{u_enc}:{_url_quote(pw, safe=safe)}"
    return f"{scheme}://{userinfo_enc}@{hostpart}"


def _cursor(conn):
    """Return a dict-like cursor for the connection."""
    if _HAS_PSYCOPG and "psycopg" in type(conn).__module__.lower():
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def _execute(conn, is_pg: bool, sql: str, params: Any = None):
    if is_pg:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(_q(sql, True), params or ())
        return cur
    return conn.execute(_q(sql, False), params or ())


def _table_columns(conn, table: str, is_pg: bool):
    if is_pg:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT column_name as name FROM information_schema.columns "
            "WHERE table_name = %s",
            (table,),
        )
        return [r["name"] for r in cur.fetchall()]
    return [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def init_db(db_path: str) -> None:
    """Create all tables and migrate columns if they do not yet exist."""
    is_pg = _is_pg(db_path)
    with _connect(db_path) as conn:
        if is_pg:
            cur = _cursor(conn)
            for stmt in _PG_DDL:
                cur.execute(stmt)
        else:
            conn.executescript(_SQLITE_DDL)

        cols = _table_columns(conn, "listings", is_pg)
        if "fit_components" not in cols:
            _execute(conn, is_pg, "ALTER TABLE listings ADD COLUMN fit_components TEXT")
        if "scored_at" not in cols:
            _execute(conn, is_pg, "ALTER TABLE listings ADD COLUMN scored_at TEXT")

        gaps_cols = _table_columns(conn, "skill_gaps", is_pg)
        if gaps_cols and "run_id" not in gaps_cols:
            _execute(conn, is_pg, "DROP TABLE IF EXISTS skill_gaps")
            if is_pg:
                cur = _cursor(conn)
                cur.execute(_PG_DDL[1])
            else:
                conn.executescript(
                    """
                    CREATE TABLE skill_gaps (
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id            TEXT NOT NULL,
                        computed_at       TEXT NOT NULL,
                        skill             TEXT NOT NULL,
                        listings_blocked  INTEGER NOT NULL,
                        opportunity_cost  REAL NOT NULL,
                        mean_score        REAL NOT NULL,
                        top_score         INTEGER,
                        example_ids       TEXT NOT NULL,
                        also_nice_to_have INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )


def make_listing_id(source: str, url: str) -> str:
    """Return a stable hash used as the listing primary key."""
    raw = f"{source}::{url}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def upsert_listings(db_path: str, rows: list[dict[str, Any]]) -> int:
    """Insert listings, ignoring rows whose id already exists. Returns new count."""
    if not rows:
        return 0
    is_pg = _is_pg(db_path)
    params_list = [
        (
            r["id"], r["title"], r["company"], r["location"], r["url"],
            r.get("description"), r["source"], r.get("posted_at"),
            r["fetched_at"], r.get("fit_score"), r.get("fit_reason"),
        )
        for r in rows
    ]

    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q("SELECT COUNT(*) FROM listings", is_pg))
        before = _row_scalar(cur.fetchone(), is_pg) or 0

        if is_pg:
            sql = """
                INSERT INTO listings
                    (id, title, company, location, url, description,
                     source, posted_at, fetched_at, fit_score, fit_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """
            cur.executemany(sql, params_list)
        else:
            sql = """
                INSERT OR IGNORE INTO listings
                    (id, title, company, location, url, description,
                     source, posted_at, fetched_at, fit_score, fit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.executemany(sql, params_list)

        cur.execute(_q("SELECT COUNT(*) FROM listings", is_pg))
        after = _row_scalar(cur.fetchone(), is_pg) or 0

    return after - before


def count_unscored(db_path: str) -> int:
    """Return the number of listings that have not yet been scored."""
    is_pg = _is_pg(db_path)
    sql = "SELECT COUNT(*) FROM listings WHERE fit_score IS NULL"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return _row_scalar(row, is_pg) or 0


def get_unscored_listings(db_path: str, limit: int = 25) -> list[dict[str, Any]]:
    """Return up to limit listings where fit_score is NULL."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT id, title, company, location, url, description,
               source, posted_at, fetched_at
        FROM   listings
        WHERE  fit_score IS NULL
        ORDER  BY fetched_at ASC
        LIMIT  ?
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), (limit,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def update_score(
    db_path: str,
    listing_id: str,
    score: int,
    reason: str,
    components: dict[str, Any] | None = None,
    scored_at: str | None = None,
) -> None:
    """Write fit_score, fit_reason, fit_components, scored_at for a listing."""
    if scored_at is None:
        scored_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    comp_json = json.dumps(components) if components is not None else None
    is_pg = _is_pg(db_path)
    sql = """
        UPDATE listings
        SET fit_score = ?, fit_reason = ?, fit_components = ?, scored_at = ?
        WHERE id = ?
    """
    with _connect(db_path) as conn:
        _execute(conn, is_pg, sql, (score, reason, comp_json, scored_at, listing_id))


def last_fetch_time(db_path: str) -> str | None:
    """Return the most recent fetched_at timestamp, or None if no rows exist."""
    is_pg = _is_pg(db_path)
    sql = "SELECT MAX(fetched_at) FROM listings"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return _row_scalar(row, is_pg)


def log_cycle(
    db_path: str,
    agent: str,
    started_at: str,
    finished_at: str,
    records_touched: int,
    status: str,
    notes: str | None = None,
) -> None:
    """Write one row to cycle_log for the completed agent run."""
    is_pg = _is_pg(db_path)
    sql = """
        INSERT INTO cycle_log
            (agent, started_at, finished_at, records_touched, status, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    with _connect(db_path) as conn:
        _execute(conn, is_pg, sql, (agent, started_at, finished_at, records_touched, status, notes))


def get_listings(
    db_path: str,
    limit: int = 100,
    min_score: int | None = None,
) -> list[dict[str, Any]]:
    """Return listings ordered by fit_score descending, optionally filtered."""
    is_pg = _is_pg(db_path)
    params: list[Any] = []
    where = ""
    if min_score is not None:
        where = "WHERE fit_score >= ?"
        params.append(min_score)
    params.append(limit)
    sql = f"""
        SELECT id, title, company, location, url,
               fit_score, fit_reason, posted_at, fetched_at
        FROM   listings
        {where}
        ORDER  BY fit_score DESC NULLS LAST
        LIMIT  ?
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_extraction(db_path: str, description_hash: str) -> dict[str, Any] | None:
    """Return a cached extraction payload, or None on a miss."""
    is_pg = _is_pg(db_path)
    sql = "SELECT payload FROM extraction_cache WHERE description_hash = ?"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), (description_hash,))
        row = cur.fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])


def put_extraction(
    db_path: str,
    description_hash: str,
    payload: dict[str, Any],
) -> None:
    """Store an extraction payload keyed on the description hash."""
    is_pg = _is_pg(db_path)
    extracted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload_json = json.dumps(payload)
    with _connect(db_path) as conn:
        if is_pg:
            sql = """
                INSERT INTO extraction_cache
                    (description_hash, payload, extracted_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (description_hash) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    extracted_at = EXCLUDED.extracted_at
            """
            cur = _cursor(conn)
            cur.execute(sql, (description_hash, payload_json, extracted_at))
        else:
            sql = """
                INSERT OR REPLACE INTO extraction_cache
                    (description_hash, payload, extracted_at)
                VALUES (?, ?, ?)
            """
            conn.execute(sql, (description_hash, payload_json, extracted_at))


def get_all_extracted_skills(db_path: str) -> list[list[str]]:
    """Return a list of required_skills lists from all cached extractions."""
    is_pg = _is_pg(db_path)
    sql = "SELECT payload FROM extraction_cache"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        rows = cur.fetchall()
    results: list[list[str]] = []
    for r in rows:
        try:
            data = json.loads(r["payload"])
            skills = data.get("required_skills")
            if isinstance(skills, list):
                results.append(skills)
        except Exception:
            continue
    return results


def count_scored(db_path: str) -> int:
    """Return the number of listings that have a fit_score."""
    is_pg = _is_pg(db_path)
    sql = "SELECT COUNT(*) FROM listings WHERE fit_score IS NOT NULL"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return _row_scalar(row, is_pg) or 0


def get_scored_listings_with_extractions(db_path: str) -> list[dict[str, Any]]:
    """Return all scored listings paired with their extracted facts."""
    is_pg = _is_pg(db_path)
    sql_listings = """
        SELECT id, title, company, location, url, description,
               fit_score, fit_reason, fit_components, posted_at, fetched_at
        FROM listings
        WHERE fit_score IS NOT NULL
        ORDER BY fit_score DESC
    """
    sql_cache = "SELECT description_hash, payload, extracted_at FROM extraction_cache"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql_listings, is_pg))
        listings_rows = cur.fetchall()
        cur.execute(_q(sql_cache, is_pg))
        cache_rows = cur.fetchall()

    cache_map = {}
    for r in cache_rows:
        try:
            cache_map[r["description_hash"]] = json.loads(r["payload"])
        except Exception:
            continue

    out = []
    for r in listings_rows:
        item = dict(r)
        desc = item.get("description") or ""
        d_hash = hashlib.sha256(desc.encode("utf-8")).hexdigest()
        item["facts"] = cache_map.get(d_hash, {})
        out.append(item)
    return out


def save_gap_snapshot(
    db_path: str,
    run_id: str,
    computed_at: str,
    rows: list[dict[str, Any]],
) -> None:
    """Write a timestamped snapshot of gap rows to skill_gaps (rule 25)."""
    if not rows:
        return
    is_pg = _is_pg(db_path)
    sql = """
        INSERT INTO skill_gaps
            (run_id, computed_at, skill, listings_blocked,
             opportunity_cost, mean_score, top_score, example_ids, also_nice_to_have)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    insert_data = [
        (
            run_id, computed_at, r["skill"], r["listings_blocked"],
            r["opportunity_cost"], r["mean_score"], r.get("top_score"),
            json.dumps(r["example_ids"]) if isinstance(r["example_ids"], list) else str(r["example_ids"]),
            r.get("also_nice_to_have", 0),
        )
        for r in rows
    ]
    with _connect(db_path) as conn:
        if is_pg:
            cur = _cursor(conn)
            cur.executemany(_q(sql, True), insert_data)
        else:
            conn.executemany(sql, insert_data)


def get_latest_gap_snapshot(db_path: str) -> list[dict[str, Any]]:
    """Return the most recent snapshot rows, ordered by opportunity_cost DESC."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT run_id, computed_at, skill, listings_blocked,
               opportunity_cost, mean_score, top_score, example_ids, also_nice_to_have
        FROM skill_gaps
        WHERE run_id = (SELECT run_id FROM skill_gaps ORDER BY id DESC LIMIT 1)
        ORDER BY opportunity_cost DESC
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        rows = cur.fetchall()
    return _rows_to_dicts(rows)


def get_gap_snapshot_runs(db_path: str) -> list[dict[str, Any]]:
    """Return distinct run_id + computed_at ordered chronologically."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT run_id, computed_at, MIN(id) as first_id
        FROM skill_gaps
        GROUP BY run_id, computed_at
        ORDER BY first_id ASC
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        rows = cur.fetchall()
    return [{"run_id": r["run_id"], "computed_at": r["computed_at"]} for r in rows]


def get_gap_snapshot_by_run(db_path: str, run_id: str) -> list[dict[str, Any]]:
    """Return skill_gaps rows for a run_id ordered by opportunity_cost DESC."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT run_id, computed_at, skill, listings_blocked,
               opportunity_cost, mean_score, top_score, example_ids, also_nice_to_have
        FROM skill_gaps
        WHERE run_id = ?
        ORDER BY opportunity_cost DESC
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), (run_id,))
        rows = cur.fetchall()
    return _rows_to_dicts(rows)


def last_gap_computed_at(db_path: str) -> str | None:
    """Return the most recent computed_at from skill_gaps, or None."""
    is_pg = _is_pg(db_path)
    sql = "SELECT MAX(computed_at) FROM skill_gaps"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return _row_scalar(row, is_pg)


def last_scored_at(db_path: str) -> str | None:
    """Return the most recent scored_at across all listings, or None."""
    is_pg = _is_pg(db_path)
    sql = "SELECT MAX(scored_at) FROM listings WHERE scored_at IS NOT NULL"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return _row_scalar(row, is_pg)


def last_cycle_summary(db_path: str) -> dict[str, Any] | None:
    """Return the most recent cycle_log row with agent='cycle_summary', or None."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT agent, started_at, finished_at, status, notes
        FROM cycle_log
        WHERE agent = 'cycle_summary'
        ORDER BY id DESC
        LIMIT 1
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return dict(row) if row else None


def clear_scores(db_path: str, limit: int) -> int:
    """Null-out scores/fields for the most recently scored listings."""
    is_pg = _is_pg(db_path)
    ids_sql = """
        SELECT id FROM listings
        WHERE fit_score IS NOT NULL
        ORDER BY scored_at DESC
        LIMIT ?
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(ids_sql, is_pg), (limit,))
        ids = [r["id"] for r in cur.fetchall()]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    update_sql = f"""
        UPDATE listings
        SET fit_score=NULL, fit_reason=NULL, fit_components=NULL, scored_at=NULL
        WHERE id IN ({placeholders})
    """
    with _connect(db_path) as conn:
        _execute(conn, is_pg, update_sql, ids)
    return len(ids)


def latest_gap_run_id(db_path: str) -> str | None:
    """Return the run_id of the most recent gap snapshot, or None."""
    is_pg = _is_pg(db_path)
    sql = "SELECT run_id FROM skill_gaps ORDER BY id DESC LIMIT 1"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return _row_scalar(row, is_pg)


def recent_cycle_logs(db_path: str, limit: int = 30) -> list[dict[str, Any]]:
    """Return the most recent `limit` cycle_summary rows, newest first."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT id, agent, started_at, finished_at,
               records_touched, status, notes
        FROM cycle_log
        WHERE agent = 'cycle_summary'
        ORDER BY id DESC
        LIMIT ?
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), (limit,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def last_verified_cycle(db_path: str) -> dict[str, Any] | None:
    """Return the most recent passing cycle_summary row (rule 38 guard)."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT agent, started_at, finished_at, status, notes
        FROM cycle_log
        WHERE agent = 'cycle_summary'
          AND status = 'ok'
        ORDER BY id DESC
        LIMIT 1
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Query Tool Storage Helpers (Rule 2: all reads through storage)
# ---------------------------------------------------------------------------


def get_companies_hiring(db_path: str, cutoff_date: str) -> list[dict[str, Any]]:
    """Return companies and listing counts for postings on or after cutoff_date."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT company, COUNT(*) as listing_count,
               MAX(COALESCE(posted_at, fetched_at)) as latest_posted_at
        FROM listings
        WHERE (posted_at >= ? OR (posted_at IS NULL AND fetched_at >= ?))
        GROUP BY company
        ORDER BY listing_count DESC, company ASC
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), (cutoff_date, cutoff_date))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_best_matches(db_path: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return highest-scoring listings with score, title, company, reason, url."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT id, title, company, location, url,
               fit_score, fit_reason, posted_at, fetched_at
        FROM listings
        WHERE fit_score IS NOT NULL
        ORDER BY fit_score DESC, title ASC
        LIMIT ?
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), (limit,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_listings_by_ids(db_path: str, ids: list[str]) -> list[dict[str, Any]]:
    """Return listings matching a list of listing IDs."""
    if not ids:
        return []
    is_pg = _is_pg(db_path)
    placeholders = ",".join("?" * len(ids))
    sql = f"""
        SELECT id, title, company, location, url,
               fit_score, fit_reason, posted_at, fetched_at
        FROM listings
        WHERE id IN ({placeholders})
        ORDER BY fit_score DESC NULLS LAST
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), ids)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def count_total_listings(db_path: str) -> int:
    """Return total count of listings in the database."""
    is_pg = _is_pg(db_path)
    sql = "SELECT COUNT(*) FROM listings"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        row = cur.fetchone()
    return _row_scalar(row, is_pg) or 0


def get_all_extractions_with_metadata(db_path: str) -> list[dict[str, Any]]:
    """Return all cached extractions with payloads and metadata."""
    is_pg = _is_pg(db_path)
    sql = "SELECT description_hash, payload, extracted_at FROM extraction_cache"
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg))
        rows = cur.fetchall()
    results = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
            results.append({
                "description_hash": r["description_hash"],
                "payload": payload,
                "extracted_at": r["extracted_at"],
            })
        except Exception:
            continue
    return results


def log_query(
    db_path: str,
    question: str,
    tool_chosen: str | None,
    params: dict[str, Any] | None,
    answerable: bool,
    duration_ms: float,
) -> None:
    """Log a natural language query interaction to query_log."""
    is_pg = _is_pg(db_path)
    sql = """
        INSERT INTO query_log (question, tool_chosen, params, answerable, duration_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    params_json = json.dumps(params) if params is not None else None
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        _execute(
            conn, is_pg, sql,
            (question, tool_chosen, params_json, 1 if answerable else 0, duration_ms, created_at),
        )


def get_recent_queries(db_path: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return recent queries logged to query_log."""
    is_pg = _is_pg(db_path)
    sql = """
        SELECT id, question, tool_chosen, params, answerable, duration_ms, created_at
        FROM query_log
        ORDER BY id DESC
        LIMIT ?
    """
    with _connect(db_path) as conn:
        cur = _cursor(conn)
        cur.execute(_q(sql, is_pg), (limit,))
        rows = cur.fetchall()
    results = []
    for r in rows:
        d = dict(r)
        if d.get("params"):
            try:
                d["params"] = json.loads(d["params"])
            except Exception:
                pass
        results.append(d)
    return results


# ---------------------------------------------------------------------------
# CLI entry point — python -m edgedash.storage --migrate
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="edgedash.storage",
        description="EdgeDash storage CLI — migrate tables, verify connectivity.",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Create tables and migrate columns (idempotent).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Override db_path / connection string. Defaults to config.yaml + DATABASE_URL.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Connect, run init_db, then print row counts for each table.",
    )
    args = parser.parse_args()

    if not args.migrate and not args.check:
        parser.print_help()
        sys.exit(0)

    if args.db:
        conn_spec = args.db
    else:
        from edgedash.config import load_config
        cfg = load_config()
        conn_spec = cfg.db_path

    is_pg = _is_pg(conn_spec)
    display = "postgres" if is_pg else "sqlite"
    if is_pg:
        try:
            p = urlparse(_normalize_pg_url(conn_spec))
            host = p.hostname or "?"
            port = f":{p.port}" if p.port else ""
            user_mask = ("*" * len(p.username)) if p.username else ""
            pw_len = len(p.password or "")
            pw_mask = (":" + "*" * min(pw_len, 8)) if pw_len else ""
            masked = f"{user_mask}{pw_mask}@{host}{port}{p.path or ''}"
        except Exception:
            masked = "..." + conn_spec[-40:]
    else:
        masked = conn_spec
    print(f"[storage] dialect={display} target={masked}")

    init_db(conn_spec)
    print("[storage] init_db complete — tables ready.")

    if args.check:
        with _connect(conn_spec) as conn:
            cur = _cursor(conn)
            tables = [
                "listings", "skill_gaps", "cycle_log",
                "extraction_cache", "query_log",
            ]
            for t in tables:
                cur.execute(_q(f"SELECT COUNT(*) FROM {t}", is_pg))
                n = _row_scalar(cur.fetchone(), is_pg) or 0
                print(f"  {t:<18} {n:>6} rows")

    print("[storage] done.")


if __name__ == "__main__":
    _main()
