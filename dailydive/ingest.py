"""Fetching, politely.

Every request honors robots.txt, identifies itself with a contact address,
rate-limits to one request per second per host, and sends conditional-GET
headers so a normal morning re-fetches almost nothing. These are the terms on
which an aggregator gets to keep existing.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from . import brand, store
from .models import Source, SourceType

log = logging.getLogger(__name__)

# A crawler that can't be contacted is a crawler that gets blocked instead of
# emailed. The address lives in brand.py so the User-Agent, the about page, and
# the removal policy can never drift apart.
CONTACT = brand.CONTACT_EMAIL
USER_AGENT = f"{brand.BOT_NAME}/{brand.BOT_VERSION} (+{brand.SITE_URL}; {CONTACT})"

MIN_INTERVAL_PER_HOST = 1.0
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class RobotsDisallowed(RuntimeError):
    """The site's robots.txt says not to fetch this. Respect it."""


class MissingCredential(RuntimeError):
    """A source needs an API key that isn't in the environment."""


# env var -> the source types that need it. Keys are read at request time and
# never written to sources.toml, the database, or a log line.
API_KEY_ENV = {SourceType.YOUTUBE_API: "YOUTUBE_API_KEY"}


@dataclass
class FetchResult:
    source: Source
    body: bytes | None  # None means 304 Not Modified — nothing changed
    status: int
    from_cache: bool = False


class Fetcher:
    """Stateful across a run so rate limits and robots rules are per-host."""

    def __init__(self, client: httpx.Client | None = None, *, respect_robots: bool = True) -> None:
        self._client = client or httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._respect_robots = respect_robots
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self, host: str) -> None:
        elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
        if elapsed < MIN_INTERVAL_PER_HOST:
            time.sleep(MIN_INTERVAL_PER_HOST - elapsed)
        self._last_hit[host] = time.monotonic()

    def _robots_for(self, url: str) -> RobotFileParser | None:
        """Fetch and cache a host's robots.txt.

        A host that fails to serve robots.txt is treated as permissive, which
        matches the standard: absence of a policy is not a prohibition.
        """
        parts = urlsplit(url)
        host = parts.netloc
        if host in self._robots:
            return self._robots[host]

        robots_url = f"{parts.scheme}://{host}/robots.txt"
        parser: RobotFileParser | None = None
        try:
            self._throttle(host)
            resp = self._client.get(robots_url)
            if resp.status_code == 200:
                parser = RobotFileParser()
                parser.parse(resp.text.splitlines())
        except httpx.HTTPError as exc:
            log.warning("could not read %s (%s) — treating as permissive", robots_url, exc)

        self._robots[host] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self._respect_robots:
            return True
        parser = self._robots_for(url)
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def _authorize(self, source: Source) -> str:
        """The URL to actually request, with any API key appended.

        Kept separate from source.url so the key never touches the config
        file, the cache table, or a log message — only the outbound request.
        """
        env_var = API_KEY_ENV.get(source.type)
        if env_var is None:
            return source.url
        key = os.environ.get(env_var, "").strip()
        if not key:
            raise MissingCredential(f"{source.id} needs {env_var} in the environment")
        joiner = "&" if "?" in source.url else "?"
        return f"{source.url}{joiner}key={key}"

    def fetch(self, source: Source, conn: sqlite3.Connection) -> FetchResult:
        """GET a feed, conditionally. Raises RobotsDisallowed if off-limits."""
        if not source.is_authorized_api and not self.allowed(source.url):
            raise RobotsDisallowed(f"robots.txt disallows {source.url}")

        # The cache key stays the un-keyed URL: the API key is a credential,
        # not part of the resource's identity, and it must not end up written
        # to the database alongside ETags.
        headers = store.get_cache_headers(conn, source.url)
        request_url = self._authorize(source)
        self._throttle(urlsplit(source.url).netloc)
        resp = self._client.get(request_url, headers=headers)

        if resp.status_code == 304:
            log.info("%s unchanged (304)", source.id)
            return FetchResult(source=source, body=None, status=304, from_cache=True)

        resp.raise_for_status()
        store.save_cache_headers(
            conn,
            source.url,
            resp.headers.get("ETag"),
            resp.headers.get("Last-Modified"),
        )
        return FetchResult(source=source, body=resp.content, status=resp.status_code)
