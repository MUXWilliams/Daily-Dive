"""Feed discovery helper.

Answers "is this actually a feed, and are we allowed to read it?" for a list of
candidate URLs. Exists because feed URLs can't be reasoned about from a desk —
sites move, XenForo paths vary, and some hosts block bots outright. Run it from
somewhere with real network access (the Actions workflow does).
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import feedparser
import httpx

from .ingest import TIMEOUT, USER_AGENT, Fetcher

# Candidates worth testing when no URLs are given. Trimmed as answers come in —
# settled questions move to sources.toml and drop off this list.
CANDIDATES: list[str] = [
    # Known good — the control. If this breaks, the fetcher broke, not the site.
    "https://reefbuilders.com/feed/",
    # Round 2: more reef publications that may run WordPress.
    "https://www.reefkeeping.com/feed/",
    "https://aquanerd.com/feed/",
    "https://melevsreef.com/feed",
    "https://www.marinedepot.com/blog/rss",
    "https://reefbum.com/feed/",
    "https://www.saltwatersmarts.com/feed",
]

# Sites where guessing the feed path failed but a feed may still exist. Rather
# than guess again, ask the page: HTML feed autodiscovery is a standard, and
# it's how a browser's "subscribe" button has always found feeds.
DISCOVER_TARGETS: list[str] = [
    # NOAA's reef programs — public domain data, and the wild-reef angle nobody
    # else runs daily. Every guessed path 404'd, so let the pages answer.
    "https://coralreefwatch.noaa.gov/",
    "https://coralreef.noaa.gov/",
    "https://www.coris.noaa.gov/rss/",
    "https://www.fisheries.noaa.gov/",
    # Guessed paths failed; these sites may still publish a feed elsewhere.
    "https://www.advancedaquarist.com/",
    "https://www.tidalgardens.com/",
    "https://reefs.com/",
]


class _FeedLinkParser(HTMLParser):
    """Pulls <link rel="alternate" type="application/rss+xml"> out of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.feeds: list[tuple[str, str]] = []  # (href, title)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rels = a.get("rel", "").lower().split()
        if "alternate" not in rels:
            return
        if not any(t in a.get("type", "").lower() for t in ("rss", "atom", "xml")):
            return
        if a.get("href"):
            self.feeds.append((a["href"], a.get("title", "")))


def discover_feeds(page_url: str, fetcher: Fetcher, client: httpx.Client) -> list[str]:
    """Return feed URLs advertised by a page, via HTML autodiscovery."""
    if not fetcher.allowed(page_url):
        return []
    try:
        resp = client.get(page_url)
        resp.raise_for_status()
    except httpx.HTTPError:
        return []

    parser = _FeedLinkParser()
    parser.feed(resp.text)
    seen: list[str] = []
    for href, _title in parser.feeds:
        absolute = urljoin(str(resp.url), href)
        if absolute not in seen:
            seen.append(absolute)
    return seen


@dataclass
class ProbeResult:
    url: str
    verdict: str  # ok | empty | not-a-feed | robots | http-error | network-error
    detail: str
    entries: int = 0
    title: str | None = None

    @property
    def icon(self) -> str:
        return {"ok": "✅", "empty": "⚠️", "robots": "🚫"}.get(self.verdict, "❌")


def probe_one(url: str, fetcher: Fetcher, client: httpx.Client) -> ProbeResult:
    if not fetcher.allowed(url):
        return ProbeResult(url, "robots", "robots.txt disallows this path — use an official API instead")

    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        return ProbeResult(url, "network-error", f"{type(exc).__name__}: {exc}")

    if resp.status_code >= 400:
        hint = ""
        if resp.status_code == 403:
            hint = " (bot protection, most likely — treat as 'not welcome' unless they say otherwise)"
        return ProbeResult(url, "http-error", f"HTTP {resp.status_code}{hint}")

    parsed = feedparser.parse(resp.content)
    title = (parsed.feed or {}).get("title")
    count = len(parsed.entries)

    if count:
        return ProbeResult(url, "ok", f"parses cleanly, newest: {parsed.entries[0].get('title', '?')[:70]}", count, title)
    if parsed.bozo:
        return ProbeResult(url, "not-a-feed", f"not parseable as a feed ({parsed.get('bozo_exception')})")
    return ProbeResult(url, "empty", "parses, but has no entries")


def probe(urls: list[str] | None = None, *, discover: bool = False) -> list[ProbeResult]:
    targets = list(urls or CANDIDATES)
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        with Fetcher(client=client) as fetcher:
            if discover and not urls:
                for page in DISCOVER_TARGETS:
                    for found in discover_feeds(page, fetcher, client):
                        if found not in targets:
                            targets.append(found)
            return [probe_one(u, fetcher, client) for u in targets]
    finally:
        client.close()


def format_markdown(results: list[ProbeResult]) -> str:
    lines = ["| URL | Result | Items | Detail |", "|---|---|---|---|"]
    for r in results:
        detail = r.detail.replace("|", "\\|")[:120]
        lines.append(f"| `{r.url}` | {r.icon} {r.verdict} | {r.entries or ''} | {detail} |")
    return "\n".join(lines)
