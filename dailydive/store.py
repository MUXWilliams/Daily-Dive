"""SQLite state: the seen-items archive and the HTTP conditional-GET cache.

Committed to git on purpose. It is free, it versions the archive for free, and
at this volume it will not outgrow the repo for years.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import Item

DEFAULT_DB = Path("dailydive.sqlite3")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    uid           TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    author        TEXT,
    raw_text      TEXT,
    category_hint TEXT,
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_published ON items (published_at DESC);
CREATE INDEX IF NOT EXISTS items_first_seen ON items (first_seen_at DESC);

-- What actually reached a page, as opposed to what was merely fetched.
--
-- The items table above is a SEEN log: everything the crawler pulls goes in,
-- including what scoring dropped as irrelevant or promotional. Of the first
-- 644 rows, 24 were ever published. So "have we run this before?" cannot be
-- answered from it, and an editor's pick checked against it would be rejected
-- for a story a machine glanced at and discarded.
--
-- Separate table rather than a column on items, because a published item is
-- not necessarily one the crawler ever fetched — a pick comes from a source
-- the crawler cannot reach at all.
CREATE TABLE IF NOT EXISTS published (
    uid        TEXT PRIMARY KEY,
    issue_date TEXT NOT NULL,
    url        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS published_issue ON published (issue_date DESC);

-- Conditional GET cache, so a normal morning re-fetches almost nothing.
CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);
"""


@contextmanager
def connect(path: Path = DEFAULT_DB) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def known_uids(conn: sqlite3.Connection, uids: Iterable[str]) -> set[str]:
    """Which of these have we already recorded? Drives dedupe across runs."""
    uids = list(uids)
    if not uids:
        return set()
    marks = ",".join("?" * len(uids))
    rows = conn.execute(f"SELECT uid FROM items WHERE uid IN ({marks})", uids)
    return {r["uid"] for r in rows}


def record_items(conn: sqlite3.Connection, items: Iterable[Item]) -> int:
    """Insert items not seen before. Returns how many were genuinely new."""
    now = datetime.now(UTC).isoformat()
    rows = [
        (
            i.uid,
            i.source_id,
            i.source_name,
            i.title,
            i.url,
            i.canonical_url,
            i.published_at.isoformat(),
            i.author,
            i.raw_text,
            i.category_hint.value if i.category_hint else None,
            now,
        )
        for i in items
    ]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO items (uid, source_id, source_name, title, url, "
        "canonical_url, published_at, author, raw_text, category_hint, first_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return conn.total_changes - before


def get_cache_headers(conn: sqlite3.Connection, url: str) -> dict[str, str]:
    """Build the If-None-Match / If-Modified-Since headers for a URL."""
    row = conn.execute("SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)).fetchone()
    if not row:
        return {}
    headers = {}
    if row["etag"]:
        headers["If-None-Match"] = row["etag"]
    if row["last_modified"]:
        headers["If-Modified-Since"] = row["last_modified"]
    return headers


def save_cache_headers(conn: sqlite3.Connection, url: str, etag: str | None, last_modified: str | None) -> None:
    conn.execute(
        "INSERT INTO http_cache (url, etag, last_modified, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, "
        "last_modified=excluded.last_modified, fetched_at=excluded.fetched_at",
        (url, etag, last_modified, datetime.now(UTC).isoformat()),
    )


def record_published(conn: sqlite3.Connection, items: Iterable[Item], issue_date: datetime) -> int:
    """Note that these items reached a page. Returns how many were new.

    Called after the issue is built, not before — the point of this table is
    that it means published, and recording an intention would make it mean
    something weaker.
    """
    rows = [(i.uid, issue_date.date().isoformat(), i.canonical_url) for i in items]
    before = conn.execute("SELECT COUNT(*) FROM published").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO published (uid, issue_date, url) VALUES (?, ?, ?)", rows
    )
    return conn.execute("SELECT COUNT(*) FROM published").fetchone()[0] - before


def published_uids(conn: sqlite3.Connection) -> set[str]:
    """Everything that has ever appeared in an issue."""
    return {row[0] for row in conn.execute("SELECT uid FROM published")}
