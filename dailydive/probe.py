"""Feed discovery helper.

Answers "is this actually a feed, and are we allowed to read it?" for a list of
candidate URLs. Exists because feed URLs can't be reasoned about from a desk —
sites move, XenForo paths vary, and some hosts block bots outright. Run it from
somewhere with real network access (the Actions workflow does).
"""

from __future__ import annotations

from dataclasses import dataclass

import feedparser
import httpx

from .ingest import TIMEOUT, USER_AGENT, Fetcher

# Candidates worth testing when no URLs are given. Grouped by what we're trying
# to learn; update as answers come in.
CANDIDATES: list[str] = [
    # Known good — the control. If this breaks, the fetcher broke, not the site.
    "https://reefbuilders.com/feed/",
    # CORAL Magazine: reef2rainforest.com stopped resolving. coralmagazine.com
    # looks like the current home — find its feed.
    "https://www.coralmagazine.com/feed/",
    "https://coralmagazine.com/feed/",
    "https://www.reef2rainforest.com/feed/",
    # Reef2Reef: the site-wide XenForo path returned 403. Try the other shapes
    # XenForo exposes before concluding they block aggregators outright.
    "https://www.reef2reef.com/forums/-/index.rss",
    "https://www.reef2reef.com/whats-new/posts/index.rss",
    "https://www.reef2reef.com/index.rss",
    "https://www.reef2reef.com/forums/index.rss",
    # Other reef publications with likely WordPress feeds.
    "https://reefhobbyistmagazine.com/feed/",
    "https://www.advancedaquarist.com/feed",
    "https://reefs.com/feed/",
    "https://www.tidalgardens.com/blog/feed/",
    # NOAA — public domain, and the wild-reef angle nobody else runs daily.
    "https://coralreefwatch.noaa.gov/index.rss",
    "https://coralreef.noaa.gov/rss.xml",
    "https://www.coris.noaa.gov/rss/coris.xml",
]


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


def probe(urls: list[str] | None = None) -> list[ProbeResult]:
    targets = urls or CANDIDATES
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        with Fetcher(client=client) as fetcher:
            return [probe_one(u, fetcher, client) for u in targets]
    finally:
        client.close()


def format_markdown(results: list[ProbeResult]) -> str:
    lines = ["| URL | Result | Items | Detail |", "|---|---|---|---|"]
    for r in results:
        detail = r.detail.replace("|", "\\|")[:120]
        lines.append(f"| `{r.url}` | {r.icon} {r.verdict} | {r.entries or ''} | {detail} |")
    return "\n".join(lines)
