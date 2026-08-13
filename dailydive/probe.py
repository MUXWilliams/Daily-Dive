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
    # aquanerd.com timed out once — retrying to tell a slow host from a dead one.
    "https://aquanerd.com/feed/",
    "https://www.aquanerd.com/feed/",
    # Round 3 candidates.
    "https://www.humble.fish/feed/",
    "https://reefcentral.com/forums/external.php?type=RSS2",
    "https://www.nano-reef.com/forums/rss/",
    # Ownership-chain news pages. Corporate sites often publish a feed at a
    # conventional path without advertising it in the page's <head>, so these
    # are worth testing directly as well as via autodiscovery.
    "https://www.bertramcapital.com/news/feed/",
    "https://www.bertramcapital.com/feed/",
    "https://www.bertramcapital.com/rss",
    "https://www.apetlife.com/news/feed/",
    "https://www.apetlife.com/feed/",
    "https://www.apetlife.com/rss",
    "https://www.bulkreefsupply.com/blog/feed",
    "https://www.neptunesystems.com/feed/",
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
    # --- Industry beat -------------------------------------------------------
    # Corporate newsrooms and investor-relations pages from the ownership map in
    # docs/industry-brief.md. Ownership, leadership, and financial news breaks
    # here first, and a primary source beats anyone's summary of it.
    "https://ecotechmarine.com/company-news",
    # The top of the Aperture chain. A Bertram exit or add-on acquisition moves
    # BRS, EcoTech, Neptune and AquaIllumination simultaneously — the highest-
    # leverage single signal in the whole ownership map.
    "https://www.bertramcapital.com/news",
    "https://www.bertramcapital.com/news/",
    "https://www.apetlife.com/news",
    "https://www.apetlife.com/news/",
    "https://www.apetlife.com/",
    "https://www.bertramcapital.com/portfolio/aperture-pet-life",
    "https://maxspect.com/en/press-releases-patents/",
    "https://www.iwakipumps.co.jp/en/ir/",
    "https://www.iwakipumps.co.jp/en/",
    "https://tunze.com/",
    "https://www.sicce.com/en/",
    "https://abyzz.com/",
    "https://www.royalexclusiv.com/",
    "https://www.panta-rhei-aquatics.com/",
    "https://www.panworldpump.com/",
    # Pet-industry trade press — where private-equity moves in this sector get
    # reported, since most of these manufacturers publish nothing themselves.
    "https://petage.com/",
    "https://www.petbusiness.com/",
    "https://www.petproductnews.com/",
    "https://www.pettradenews.com/",
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


# WordPress advertises several feeds that are never what we want: comment
# streams, oEmbed endpoints, and Web Stories. Filtering them here keeps the
# probe output readable and stops a comments feed from being mistaken for news.
_JUNK_FEED_MARKERS = ("/comments/feed", "wp-json", "oembed", "web-stories", "/feed/atom", "?attachment_id=")


def discover_feeds(page_url: str, fetcher: Fetcher, client: httpx.Client) -> tuple[list[str], str]:
    """Feed URLs a page advertises, plus a note explaining an empty result.

    Returns (urls, note). The note matters: a site with no feeds and a site we
    couldn't reach both yield an empty list, and those need different responses.
    """
    if not fetcher.allowed(page_url):
        return [], "robots.txt disallows the page"
    try:
        resp = client.get(page_url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return [], f"{type(exc).__name__}: {str(exc)[:80]}"

    parser = _FeedLinkParser()
    parser.feed(resp.text)

    seen: list[str] = []
    skipped = 0
    for href, _title in parser.feeds:
        absolute = urljoin(str(resp.url), href)
        if any(marker in absolute.lower() for marker in _JUNK_FEED_MARKERS):
            skipped += 1
            continue
        if absolute not in seen:
            seen.append(absolute)

    if seen:
        return seen, ""
    if skipped:
        return [], f"advertises {skipped} feed(s), all comments/oembed boilerplate"
    return [], "page loads but advertises no feed"


@dataclass
class ProbeResult:
    url: str
    verdict: str  # ok | empty | not-a-feed | robots | http-error | network-error
    detail: str
    entries: int = 0
    title: str | None = None

    @property
    def icon(self) -> str:
        return {"ok": "✅", "empty": "⚠️", "robots": "🚫", "no-feed": "➖"}.get(self.verdict, "❌")


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
    dead_ends: list[ProbeResult] = []
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        with Fetcher(client=client) as fetcher:
            if discover and not urls:
                for page in DISCOVER_TARGETS:
                    found, note = discover_feeds(page, fetcher, client)
                    for feed_url in found:
                        if feed_url not in targets:
                            targets.append(feed_url)
                    if not found:
                        # Report rather than silently drop: "no feed here" is a
                        # finding, and distinguishes a dead site from a quiet one.
                        dead_ends.append(ProbeResult(page, "no-feed", note))
            return [probe_one(u, fetcher, client) for u in targets] + dead_ends
    finally:
        client.close()


def format_markdown(results: list[ProbeResult]) -> str:
    lines = ["| URL | Result | Items | Detail |", "|---|---|---|---|"]
    for r in results:
        detail = r.detail.replace("|", "\\|")[:120]
        lines.append(f"| `{r.url}` | {r.icon} {r.verdict} | {r.entries or ''} | {detail} |")
    return "\n".join(lines)
