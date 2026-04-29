"""SQLite cache for scraped pages and parsed results.

Past-season data (data_year < current year) is cached forever.
Current-season or unknown-year data uses a 24h TTL.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".cache" / "mtb-mcp" / "cache.db"
_TTL_SECONDS = 24 * 60 * 60  # 24h for current-season / unknown
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db_path() -> Path:
    env = os.environ.get("MTB_CACHE_DB")
    return Path(env) if env else _DEFAULT_DB


def _now() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _current_year() -> int:
    return dt.datetime.now(dt.timezone.utc).year


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_cache (
            url        TEXT PRIMARY KEY,
            html       TEXT NOT NULL,
            fetched_at INTEGER NOT NULL,
            data_year  INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS result_cache (
            source       TEXT NOT NULL,
            entity_type  TEXT NOT NULL,
            entity_id    TEXT NOT NULL,
            year_filter  TEXT NOT NULL,
            data         TEXT NOT NULL,
            fetched_at   INTEGER NOT NULL,
            data_year    INTEGER,
            PRIMARY KEY (source, entity_type, entity_id, year_filter)
        )
        """
    )
    _conn = conn
    return conn


def _is_fresh(fetched_at: int, data_year: int | None) -> bool:
    """Past seasons cache forever; current/unknown use TTL."""
    if data_year is not None and data_year < _current_year():
        return True
    return (_now() - fetched_at) < _TTL_SECONDS


# ---------- page cache ----------


def get_cached_page(url: str) -> str | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT html, fetched_at, data_year FROM page_cache WHERE url = ?",
            (url,),
        ).fetchone()
    if row is None:
        return None
    html, fetched_at, data_year = row
    if not _is_fresh(fetched_at, data_year):
        return None
    return html


def store_page(url: str, html: str, data_year: int | None = None) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO page_cache (url, html, fetched_at, data_year)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                html = excluded.html,
                fetched_at = excluded.fetched_at,
                data_year = excluded.data_year
            """,
            (url, html, _now(), data_year),
        )


# ---------- result cache ----------


def get_cached_results(
    source: str,
    entity_type: str,
    entity_id: str,
    year_filter: str | int | None = None,
) -> Any | None:
    yf = "" if year_filter is None else str(year_filter)
    with _lock:
        conn = _connect()
        row = conn.execute(
            """
            SELECT data, fetched_at, data_year FROM result_cache
            WHERE source = ? AND entity_type = ? AND entity_id = ? AND year_filter = ?
            """,
            (source, entity_type, entity_id, yf),
        ).fetchone()
    if row is None:
        return None
    data, fetched_at, data_year = row
    if not _is_fresh(fetched_at, data_year):
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def store_results(
    source: str,
    entity_type: str,
    entity_id: str,
    data: Any,
    year_filter: str | int | None = None,
    data_year: int | None = None,
) -> None:
    yf = "" if year_filter is None else str(year_filter)
    payload = json.dumps(data, default=str)
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO result_cache
                (source, entity_type, entity_id, year_filter, data, fetched_at, data_year)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, entity_type, entity_id, year_filter) DO UPDATE SET
                data = excluded.data,
                fetched_at = excluded.fetched_at,
                data_year = excluded.data_year
            """,
            (source, entity_type, entity_id, yf, payload, _now(), data_year),
        )


# ---------- maintenance ----------


def get_cache_stats() -> dict[str, Any]:
    with _lock:
        conn = _connect()
        page_count = conn.execute("SELECT COUNT(*) FROM page_cache").fetchone()[0]
        result_count = conn.execute("SELECT COUNT(*) FROM result_cache").fetchone()[0]
        page_bytes = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(html)), 0) FROM page_cache"
        ).fetchone()[0]
        result_bytes = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM result_cache"
        ).fetchone()[0]
        oldest = conn.execute(
            "SELECT MIN(fetched_at) FROM page_cache"
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(fetched_at) FROM page_cache"
        ).fetchone()[0]
    return {
        "db_path": str(_db_path()),
        "page_count": page_count,
        "result_count": result_count,
        "page_bytes": int(page_bytes or 0),
        "result_bytes": int(result_bytes or 0),
        "oldest_page_fetched_at": oldest,
        "newest_page_fetched_at": newest,
        "current_year": _current_year(),
        "ttl_seconds": _TTL_SECONDS,
    }


def invalidate_current_season() -> dict[str, int]:
    """Drop cache entries tagged with the current year (or untagged)."""
    year = _current_year()
    with _lock:
        conn = _connect()
        pages = conn.execute(
            "DELETE FROM page_cache WHERE data_year IS NULL OR data_year >= ?",
            (year,),
        ).rowcount
        results = conn.execute(
            "DELETE FROM result_cache WHERE data_year IS NULL OR data_year >= ?",
            (year,),
        ).rowcount
    return {"pages_deleted": pages, "results_deleted": results}


def invalidate_url(url: str) -> int:
    with _lock:
        conn = _connect()
        return conn.execute("DELETE FROM page_cache WHERE url = ?", (url,)).rowcount


def clear_all_cache() -> dict[str, int]:
    with _lock:
        conn = _connect()
        pages = conn.execute("DELETE FROM page_cache").rowcount
        results = conn.execute("DELETE FROM result_cache").rowcount
    return {"pages_deleted": pages, "results_deleted": results}
