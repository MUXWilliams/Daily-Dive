"""Feed discovery helper.

Answers "is this actually a feed, and are we allowed to read it?" for a list of
candidate URLs. Exists because feed URLs can't be reasoned about from a desk —
sites move, XenForo paths vary, and some hosts block bots outright. Run it from
somewhere with real network access (the Actions workflow does).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

import feedparser
import httpx

from . import ingest
from .ingest import TIMEOUT, USER_AGENT, Fetcher

# Candidates worth testing when no URLs are given. Trimmed as answers come in —
# settled questions move to sources.toml and drop off this list.
CANDIDATES: list[str] = [
    # Known good — the control. If this breaks, the fetcher broke, not the site.
    "https://reefbuilders.com/feed/",
    # aquanerd.com timed out once — retrying to tell a slow host from a dead one.
    "https://aquanerd.com/feed/",
    "https://www.aquanerd.com/feed/",
    # Trade press: the www hosts all failed DNS resolution, which usually means
    # the bare domain is the real one. Retrying without the subdomain.
    "https://petbusiness.com/feed/",
    "https://petproductnews.com/feed/",
    "https://pettradenews.com/feed/",
    "https://www.petfoodindustry.com/rss/articles",
    # --- Leads from the aggregation-strategy doc -----------------------------
    # Reef2Reef per-subforum path. The four site-wide shapes all 403'd, which
    # reads as a site-level block rather than a wrong path — but a per-forum URL
    # is a shape we haven't actually tried, and one request settles it.
    "https://www.reef2reef.com/forums/general-reef-discussion.51/index.rss",
    # Public-aquarium science blogs: conservation and reef science from
    # institutions. Wild Reefs is the thinnest section in the issue today.
    "https://www.calacademy.org/feed",
    "https://www.waikikiaquarium.org/feed/",
    "https://www.montereybayaquarium.org/feed/",
    "https://www.aqua.org/blog/feed",
    # Clubs. Events has never carried a single item because no club source
    # exists. Most clubs run WordPress or publish an iCal calendar.
    "https://scmas.org/feed/",
    "https://dfwmas.org/feed/",
    "https://atlantareefclub.org/feed/",
    "https://www.wamas.org/feed/",
    # --- Reef science --------------------------------------------------------
    # Asked for as "can we read @ICRSCoralReefs on x.com". We can't — X has no
    # free read tier and killed RSS — but ICRS is a society, and its X account
    # is a distribution channel rather than the origin. These are the origins.
    "https://coralreefs.org/feed/",
    "https://coralreefs.org/news/feed/",
    # The society's journal, published by Springer, which serves per-journal
    # RSS. Primary literature, and Wild Reefs is the thinnest section we have.
    "https://link.springer.com/search.rss?facet-journal-id=338",
    # Bluesky serves free public RSS per profile, no key and no auth — the one
    # microblog readable without paying or misbehaving. The handles are
    # guesses; a 404 costs one request and settles it.
    "https://bsky.app/profile/icrs.bsky.social/rss",
    "https://bsky.app/profile/coralreefs.bsky.social/rss",
    # Ten more Bluesky accounts, supplied by the editor. Probed rather than
    # configured straight in, for two reasons: the age column says which are
    # actually posting, and the feed-title column gives each account's real
    # display name — the name it gets CREDITED by — instead of one typed
    # from memory.
    "https://bsky.app/profile/scrippsocean.bsky.social/rss",
    "https://bsky.app/profile/austsocfishbiol.bsky.social/rss",
    "https://bsky.app/profile/greatsouthernreef.bsky.social/rss",
    "https://bsky.app/profile/uaf-oarc-alaska.bsky.social/rss",
    "https://bsky.app/profile/projectseahorse.bsky.social/rss",
    "https://bsky.app/profile/vibriosoup.bsky.social/rss",
    "https://bsky.app/profile/coralcitycamera.bsky.social/rss",
    "https://bsky.app/profile/acarreiro.bsky.social/rss",
    "https://bsky.app/profile/ubcoceans.bsky.social/rss",
    "https://bsky.app/profile/uncw-cms.bsky.social/rss",
    # --- Open-access journals ------------------------------------------------
    # JZAR is the closest of these to the hobby: public-aquarium husbandry,
    # life-support systems and quarantine, which is the same craft at a larger
    # scale. Diamond open access on OJS, whose standard feed path this is.
    "https://jzar.org/jzar/gateway/plugin/WebFeedGatewayPlugin/rss2",
    # Frontiers publishes on the order of a hundred marine papers a month, most
    # of them fisheries and oceanography. Probing to see the shape and volume
    # before deciding whether a whole-journal feed is usable at all.
    "https://www.frontiersin.org/journals/marine-science/rss",
    # JZAR, second attempt. The path above 404s, so the journal is not at the
    # bare-slug OJS shape. These are the other two layouts OJS ships with —
    # index.php-prefixed, and single-journal installs that drop the slug.
    "https://jzar.org/index.php/jzar/gateway/plugin/WebFeedGatewayPlugin/rss2",
    "https://jzar.org/jzar/gateway/plugin/WebFeedGatewayPlugin/atom",
    # --- El Nino and ENSO ----------------------------------------------------
    # Asked for by name, and there is no source in sources.toml that covers it:
    # the Bluesky oceanography accounts mention ENSO in passing but nothing
    # tracks the forecast itself. NOAA is the primary and its work is public
    # domain, so this is the cheapest good source in the whole file if a feed
    # exists. The ENSO blog is the readable one; CPC's advisory is the official
    # one. A warm-phase forecast is a bleaching forecast, six months early.
    "https://www.climate.gov/news-features/department/enso-blog/feed",
    "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_advisory/rss.xml",
    "https://www.pmel.noaa.gov/elnino/rss.xml",
    # --- OpenAlex ------------------------------------------------------------
    # The direct answer to "which papers came out this week", across every
    # journal at once rather than one journal at a time. Free, no key, and the
    # mailto puts us in their polite pool, which is their documented ask.
    #
    # Probed rather than configured because the filter syntax cannot be checked
    # from a desk and a wrong filter fails quietly: OpenAlex answers 200 with
    # an error body and no results, so a broken query looks exactly like a
    # quiet week. probe._probe_json exists to tell those two apart.
    #
    # Note for anyone working from the source document: its example uses
    # `concepts.id`, which OpenAlex has deprecated in favour of `topics`. These
    # use a title/abstract search instead, which needs no ID lookup and does
    # not silently return nothing when an ID moves.
    (
        "https://api.openalex.org/works"
        "?filter=title_and_abstract.search:coral,from_publication_date:{since},is_oa:true"
        "&sort=publication_date:desc&per-page=25&mailto=theloneaquarist@gmail.com"
    ),
    (
        "https://api.openalex.org/works"
        "?filter=title_and_abstract.search:reef%20aquarium%20OR%20scleractinia%20OR%20zooxanthellae"
        ",from_publication_date:{since},is_oa:true"
        "&sort=publication_date:desc&per-page=25&mailto=theloneaquarist@gmail.com"
    ),
    # SETTLED, do not re-probe: humble.fish 403s on both XenForo shapes, the
    # same wall as Reef2Reef. Maxspect's feed was found at the Joomla
    # "?format=feed&type=rss" shape and is now in sources.toml. Frontiers in
    # Marine Science answered (20 entries, 1 day old) and is now configured.
    # drumandcroaker.org loads but advertises no feed — it is a static PDF
    # archive, so reaching it needs a directory reader, not a feed parser.
    # srac.msstate.edu/factsheets.html is a 404.
]

# Sites where guessing the feed path failed but a feed may still exist. Rather
# than guess again, ask the page: HTML feed autodiscovery is a standard, and
# it's how a browser's "subscribe" button has always found feeds.
DISCOVER_TARGETS: list[str] = [
    # Guessed paths failed; these sites may still publish a feed elsewhere.
    "https://www.advancedaquarist.com/",
    "https://www.tidalgardens.com/",
    "https://reefs.com/",
    # --- Industry beat -------------------------------------------------------
    # SETTLED, do not re-probe: bertramcapital.com and apetlife.com publish no
    # feed. Every conventional path 404s and both /news pages advertise nothing.
    # Same for iwakipumps.co.jp (including /ir/), tunze.com, sicce.com,
    # abyzz.com, royalexclusiv.com and panworldpump.com. These are hand-
    # maintained HTML. See README "Watching the pages that have no feed".
    #
    # Still worth asking, since NOAA's index page lists feeds as ordinary links
    # rather than advertising them in the head — the new fallback may find them.
    "https://coralreefwatch.noaa.gov/",
    "https://www.coris.noaa.gov/rss/",
    "https://coralreef.noaa.gov/rss.html",
    "https://www.fisheries.noaa.gov/news-and-announcements/news",
    "https://petage.com/news/",
    # Clubs and institutions whose feed path we're guessing at — ask the page
    # instead of guessing a second time.
    "https://www.reefcentral.com/",
    "https://www.calacademy.org/hope-for-reefs",
    "https://www.waikikiaquarium.org/",
    "https://dfwmas.org/",
    "https://scmas.org/",
    # If the guessed feed paths miss, ask the site itself.
    "https://coralreefs.org/",
    "https://link.springer.com/journal/338",
    # Drum and Croaker is the practitioner journal for public-aquarium life
    # support. Published as static PDFs with no feed as far as anyone can tell
    # — asking the page directly rather than assuming, since a feed would make
    # it usable and its absence means it never can be under the no-scrape rule.
    "http://drumandcroaker.org/",
    "https://srac.msstate.edu/factsheets.html",
]


class _FeedLinkParser(HTMLParser):
    """Finds feeds a page points to.

    Two mechanisms, because sites use both:
      - <link rel="alternate" type="application/rss+xml"> in the head, the
        standard autodiscovery mechanism.
      - ordinary <a> links to .rss/.xml files, which is how directory pages
        like NOAA's feed index actually publish their feeds. Those are kept
        separate and only used when the head advertises nothing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.feeds: list[tuple[str, str]] = []  # (href, title)
        self.linked_files: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        href = a.get("href", "")

        if tag == "a":
            if href and href.lower().split("?")[0].endswith((".rss", ".xml", ".atom")):
                self.linked_files.append(href)
            return

        if tag != "link":
            return
        rels = a.get("rel", "").lower().split()
        if "alternate" not in rels:
            return
        if not any(t in a.get("type", "").lower() for t in ("rss", "atom", "xml")):
            return
        if href:
            self.feeds.append((href, a.get("title", "")))


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

    # Nothing in the head. Fall back to .rss/.xml files linked in the body —
    # how feed directory pages (NOAA's, for one) actually publish theirs.
    for href in parser.linked_files:
        absolute = urljoin(str(resp.url), href)
        if absolute not in seen:
            seen.append(absolute)
    if seen:
        return seen[:8], "found via linked .rss/.xml files, not head autodiscovery"

    if skipped:
        return [], f"advertises {skipped} feed(s), all comments/oembed boilerplate"
    return [], "page loads but advertises no feed"


@dataclass
class ProbeResult:
    url: str
    verdict: str  # ok | stale | empty | not-a-feed | robots | http-error | network-error
    detail: str
    entries: int = 0
    title: str | None = None

    @property
    def icon(self) -> str:
        return {"ok": "✅", "stale": "🕸️", "empty": "⚠️", "robots": "🚫", "no-feed": "➖"}.get(self.verdict, "❌")


def _newest_age_days(parsed: feedparser.FeedParserDict) -> int | None:
    """Days since the most recent dated entry, or None if nothing is dated."""
    stamps = [
        datetime(*e.published_parsed[:6], tzinfo=UTC)
        for e in parsed.entries
        if e.get("published_parsed")
    ]
    if not stamps:
        return None
    return (datetime.now(UTC) - max(stamps)).days


def _probe_json(url: str, body: bytes) -> ProbeResult | None:
    """Read a JSON API response, for the sources that aren't feeds.

    Two of the source types here speak JSON rather than RSS, and until now the
    probe could only say "not-a-feed" about them — which meant the one thing
    the probe exists for, checking a URL before configuring it, was unavailable
    for exactly the URLs whose query syntax is easiest to get wrong.

    Recognizes the OpenAlex and YouTube result shapes. Returns None when the
    body isn't JSON at all, so the caller falls through to feedparser.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    if error := (payload.get("error") or payload.get("message")):
        # OpenAlex reports a bad filter with 200 + an error body, so this is
        # the difference between "no results" and "the query is wrong".
        return ProbeResult(url, "not-a-feed", f"API error: {str(error)[:90]}")

    results = payload.get("results")
    if results is None:
        results = payload.get("items")
    if results is None:
        return None

    count = len(results)
    if not count:
        return ProbeResult(url, "empty", "valid JSON query, but it matched nothing")

    first = results[0] if isinstance(results[0], dict) else {}
    headline = first.get("display_name") or first.get("title") or "?"
    venue = ((first.get("primary_location") or {}).get("source") or {}).get("display_name")
    total = (payload.get("meta") or {}).get("count")
    detail = f"{count} returned"
    if total is not None:
        detail += f" of {total} matching"
    detail += f": {str(headline)[:60]}"
    return ProbeResult(url, "ok", detail, count, venue or "JSON API")


def probe_one(url: str, fetcher: Fetcher, client: httpx.Client) -> ProbeResult:
    if not fetcher.allowed(url):
        return ProbeResult(url, "robots", "robots.txt disallows this path — use an official API instead")

    try:
        resp = client.get(ingest.resolve_window(url))
    except httpx.HTTPError as exc:
        return ProbeResult(url, "network-error", f"{type(exc).__name__}: {exc}")

    if resp.status_code >= 400:
        hint = ""
        if resp.status_code == 403:
            hint = " (bot protection, most likely — treat as 'not welcome' unless they say otherwise)"
        return ProbeResult(url, "http-error", f"HTTP {resp.status_code}{hint}")

    if (json_result := _probe_json(url, resp.content)) is not None:
        return json_result

    parsed = feedparser.parse(resp.content)
    title = (parsed.feed or {}).get("title")
    count = len(parsed.entries)

    if count:
        # Age, not just "it parses". Four of the six sources in sources.toml
        # probed "ok" and turned out to be publishing nothing — ReefBum's
        # newest entry was three and a half years old. A feed that parses is
        # not a feed that is alive, and this is the column that tells them
        # apart before a dead source gets configured.
        age = _newest_age_days(parsed)
        stamp = "unknown age" if age is None else f"newest {age}d old"
        verdict = "ok" if (age is not None and age <= 60) else "stale"
        headline = parsed.entries[0].get("title", "?")[:60]
        return ProbeResult(url, verdict, f"{stamp}: {headline}", count, title)
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


def _cell(text: str | None, limit: int = 120) -> str:
    """One table cell. Collapses whitespace, because an error string with a
    newline in it would otherwise split a row and corrupt the whole table."""
    return " ".join((text or "").split()).replace("|", "\\|")[:limit]


def format_markdown(results: list[ProbeResult]) -> str:
    # The feed's own title is the outlet's self-description, which is the
    # name it should be CREDITED by. Reading it here beats hand-typing a name
    # into sources.toml from memory and getting an organisation's name wrong
    # on a public page.
    lines = ["| URL | Result | Items | Feed title | Detail |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| `{r.url}` | {r.icon} {r.verdict} | {r.entries or ''} "
            f"| {_cell(r.title, 60)} | {_cell(r.detail)} |"
        )
    return "\n".join(lines)
