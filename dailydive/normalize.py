"""Feed dialects in, one `Item` model out.

Every outlet's quirks get contained here so nothing downstream has to know
whether a thing came from XenForo or YouTube. If a feed entry can't produce a
creditable Item, it is dropped with a warning rather than published half-cited.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta

import feedparser

from .models import AttributionError, Category, Item, Source, SourceType

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _text(html: str | None, limit: int = 2000) -> str | None:
    """Strip markup to plain text for the later scoring passes.

    Not a sanitizer for output — nothing here is rendered as HTML. It exists so
    the model passes in v1+ get readable text instead of markup soup.
    """
    if not html:
        return None
    plain = _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()
    return plain[:limit] or None


def _published(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=UTC)
    return None


def _author(entry: feedparser.FeedParserDict) -> str | None:
    author = entry.get("author")
    if isinstance(author, str) and author.strip():
        return author.strip()
    detail = entry.get("author_detail") or {}
    name = detail.get("name") if isinstance(detail, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def _youtube_extra(entry: feedparser.FeedParserDict) -> dict[str, str]:
    """Video metadata only — no transcripts.

    There is no official transcript API for videos you don't own, so v1 works
    from what the channel's own feed publishes and sends people to the creator.
    """
    extra: dict[str, str] = {}
    media = entry.get("media_statistics") or {}
    if isinstance(media, dict) and media.get("views"):
        extra["views"] = str(media["views"])
    if entry.get("yt_videoid"):
        extra["video_id"] = str(entry["yt_videoid"])
    return extra


# A headline's worth of a post. Long enough to carry a claim, short enough to
# sit on one line next to everything else in the issue.
SYNTH_TITLE_CHARS = 110

_SENTENCE_END_RE = re.compile(r"[.!?](?=\s)|\n")

# Words whose trailing full stop is an abbreviation, not a sentence ending. The
# first live run cut headlines at every one of these — "The administration of
# U.S", "Our colleague Dr", "director Prof", "#NewStudy by Menkara et al" —
# each of which reads as a truncation bug, because it is one.
_ABBREVIATIONS = frozenset(
    """
    dr prof mr mrs ms st sr jr vs etc al inc ltd co corp dept est fig no vol
    approx cf ca ie eg pp ed eds repr trans univ assoc
    """.split()
)


def _ends_in_abbreviation(head: str) -> bool:
    """True if `head` stops on an abbreviation rather than a real sentence."""
    last = head.rstrip(".").rsplit(" ", 1)[-1] if " " in head else head.rstrip(".")
    bare = last.strip(".,;:!?\"'()[[]").lower()
    if not bare:
        return False
    # A dotted initialism ("U.S", "e.g") or a single letter is never a sentence
    # ending — a one-letter "word" is an initial, as in "Amanda V. Smith".
    if "." in last.rstrip(".") or len(bare) == 1:
        return True
    return bare in _ABBREVIATIONS


def _synth_title(text: str | None, limit: int = SYNTH_TITLE_CHARS) -> str | None:
    """A headline for something that never had one.

    Bluesky posts have body text and no title, and the renderer refuses an
    item without one — correctly, since an untitled row can't be credited or
    read. So the first sentence becomes the headline, and a post with no text
    at all (an image on its own) returns None and is dropped rather than
    published as a bare link.
    """
    plain = _text(text, limit=400)
    if not plain:
        return None

    # Prefer a sentence boundary; a truncated sentence reads as a mistake,
    # while a complete short one reads as a headline.
    for match in _SENTENCE_END_RE.finditer(plain):
        head = plain[: match.start() + 1].strip()
        if _ends_in_abbreviation(head):
            continue  # "U.S." and "Dr." are not the end of a sentence
        # Headlines don't take a full stop, but "!" and "?" carry meaning.
        head = head.rstrip(".")
        if head and len(head) <= limit:
            return head

    if len(plain) <= limit:
        return plain
    clipped = plain[:limit].rsplit(" ", 1)[0].rstrip(",;:-—")
    return f"{clipped}…" if clipped else None


def _reddit_extra(entry: feedparser.FeedParserDict) -> dict[str, str]:
    return {"subreddit": entry["source"]["title"]} if isinstance(entry.get("source"), dict) else {}


def _normalize_youtube_api(source: Source, body: bytes) -> list[Item]:
    """playlistItems.list JSON -> Items.

    One request per channel per run, against the uploads playlist, which the
    API prices at a single quota unit — five channels cost 5 of the free
    10,000/day. Deliberately playlistItems and not search.list: search costs
    100 units, returns the same information less reliably, and would put a
    daily run within sight of the quota for no benefit.

    Metadata only. There is still no official transcript API for videos you
    don't own, so this reads title, description and date, and sends people to
    the creator.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning("%s: response was not JSON (%s)", source.id, exc)
        return []

    if error := payload.get("error"):
        # Surfaced rather than swallowed: a bad key, a disabled API and an
        # exhausted quota all arrive here, and they need different fixes.
        log.error("%s: YouTube API error %s: %s", source.id, error.get("code"), error.get("message"))
        return []

    items: list[Item] = []
    for entry in payload.get("items", []):
        snippet = entry.get("snippet") or {}
        video_id = (snippet.get("resourceId") or {}).get("videoId")
        title = snippet.get("title")
        stamp = snippet.get("publishedAt")
        if not video_id or not title or not stamp:
            log.warning("%s: playlist entry missing id, title or date, dropped", source.id)
            continue

        # Private and deleted videos stay in the uploads playlist as
        # placeholders with their real title replaced. Publishing one would
        # mean linking a reader to a video they cannot watch.
        if title in {"Private video", "Deleted video"}:
            continue

        extra = {"video_id": video_id}
        if source.section:
            extra["section"] = source.section

        try:
            items.append(
                Item(
                    source_id=source.id,
                    source_name=source.display_name,
                    title=title,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    published_at=datetime.fromisoformat(stamp.replace("Z", "+00:00")),
                    author=snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle"),
                    raw_text=_text(snippet.get("description")),
                    category_hint=source.category_hint,
                    extra=extra,
                )
            )
        except (AttributionError, ValueError) as exc:
            log.warning("%s: %r dropped (%s)", source.id, title, exc)

    return items


def _inflate_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    """Rebuild an abstract from OpenAlex's inverted index.

    OpenAlex stores abstracts as {word: [positions]} rather than as text — a
    workaround for publishers who license the index but not the prose. Putting
    each word back at its positions reconstructs it, which is what every
    OpenAlex client does and what the field is for.

    Gaps are possible if positions are sparse, so this reads as best-effort
    context for the scorer rather than as text anyone will see: raw_text is
    never rendered.
    """
    if not inverted:
        return None
    positions: dict[int, str] = {}
    for word, spots in inverted.items():
        for spot in spots:
            positions[spot] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))


def _openalex_url(work: dict) -> str | None:
    """Where to send a reader for this paper.

    Preference order is deliberate. An open-access landing page is a page the
    reader can actually read; a DOI is the durable citation but may resolve to
    a paywall. The primary location comes last because it is whatever the
    publisher registered, paywall or not.

    A link nobody can open is not much of a citation, so the readable one wins
    over the canonical one.
    """
    best = work.get("best_oa_location") or {}
    for candidate in (
        best.get("landing_page_url"),
        work.get("doi"),
        (work.get("primary_location") or {}).get("landing_page_url"),
    ):
        if candidate:
            return candidate
    return None


def _normalize_openalex(source: Source, body: bytes) -> list[Item]:
    """OpenAlex /works JSON -> Items, each credited to its own journal.

    Every other source in this project has one outlet, named once in
    sources.toml. This one has as many outlets as it has results, because
    OpenAlex is an index and not a publisher: crediting a paper in Coral Reefs
    to "OpenAlex" would be exactly the miscredit the whole attribution design
    exists to prevent. So source_name is read per item from the work's own
    primary location, and a work with no identifiable journal is dropped
    rather than credited to something convenient.

    The authors line is trimmed to the first author plus "et al." — a paper
    with two hundred authors is common in this literature and would otherwise
    fill the page.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning("%s: response was not JSON (%s)", source.id, exc)
        return []

    if error := payload.get("error"):
        # A malformed filter is the likely cause and it fails silently
        # otherwise: the query returns an error object, no results, and the
        # section simply never appears.
        log.error("%s: OpenAlex error: %s — %s", source.id, error, payload.get("message", ""))
        return []

    items: list[Item] = []
    for work in payload.get("results", []):
        title = work.get("display_name") or work.get("title")
        stamp = work.get("publication_date")
        url = _openalex_url(work)
        journal = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")

        if not title or not stamp or not url:
            log.warning("%s: work missing title, date or link, dropped", source.id)
            continue
        if not journal:
            # Preprints and records with no registered venue land here. The
            # paper may be fine; we just cannot say who published it, and this
            # project does not publish a line it cannot credit.
            log.warning("%s: %r has no journal to credit, dropped", source.id, title)
            continue

        authorships = work.get("authorships") or []
        author = None
        if authorships:
            first = (authorships[0].get("author") or {}).get("display_name")
            if first:
                author = f"{first} et al." if len(authorships) > 1 else first

        extra = {"venue": journal}
        if source.section:
            extra["section"] = source.section
        if doi := work.get("doi"):
            extra["doi"] = doi

        try:
            items.append(
                Item(
                    source_id=source.id,
                    source_name=journal,
                    title=title,
                    url=url,
                    # OpenAlex dates are plain YYYY-MM-DD. Read as UTC midnight
                    # so they compare against feed timestamps at all.
                    published_at=datetime.fromisoformat(stamp).replace(tzinfo=UTC),
                    author=author,
                    raw_text=_text(_inflate_abstract(work.get("abstract_inverted_index"))),
                    category_hint=source.category_hint,
                    extra=extra,
                )
            )
        except (AttributionError, ValueError) as exc:
            log.warning("%s: %r dropped (%s)", source.id, title, exc)

    return items


def normalize(source: Source, body: bytes) -> list[Item]:
    """Parse one feed body into Items, dropping anything uncreditable."""
    if source.type is SourceType.YOUTUBE_API:
        return _normalize_youtube_api(source, body)
    if source.type is SourceType.OPENALEX:
        return _normalize_openalex(source, body)

    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        log.warning("%s: unparseable feed (%s)", source.id, parsed.get("bozo_exception"))
        return []

    items: list[Item] = []
    for entry in parsed.entries:
        url = entry.get("link")
        summary = entry.get("summary") or entry.get("description")
        title = entry.get("title")
        if source.type is SourceType.BLUESKY and not title:
            # Posts have body text and no title. The renderer refuses an
            # untitled item, correctly, so the first sentence becomes one.
            title = _synth_title(summary)
        if not url or not title:
            log.warning("%s: entry missing link or title, dropped", source.id)
            continue

        published = _published(entry)
        if published is None:
            log.warning("%s: %r has no date, dropped", source.id, title)
            continue

        extra: dict[str, str] = {}
        if source.type is SourceType.YOUTUBE:
            extra = _youtube_extra(entry)
        elif source.type is SourceType.REDDIT:
            extra = _reddit_extra(entry)
        if source.section:
            extra["section"] = source.section

        try:
            items.append(
                Item(
                    source_id=source.id,
                    source_name=source.display_name,
                    title=title,
                    url=url,
                    published_at=published,
                    author=_author(entry),
                    raw_text=_text(summary),
                    category_hint=source.category_hint,
                    extra=extra,
                )
            )
        except (AttributionError, ValueError) as exc:
            log.warning("%s: %r dropped (%s)", source.id, title, exc)

    return items


# Words too common to say two posts are about the same thing.
_STOPWORDS = frozenset(
    """
    a an the and or but of in on at to for from by with as is are was were be
    been being it its this that these those has have had will would can could
    new now our your their his her out up more most just about after before
    over under into than then them they we you i not no all some any one two
    """.split()
)

# How much of two headlines must overlap before they count as one story.
# Measured against the six posts about one new seahorse species that appeared
# in a single issue: pairs within that group scored 0.11-0.71, while unrelated
# headlines from the same issue scored 0.00 against all of them. 0.40 sits in
# that gap. It collapses most of a group rather than all of it, which is the
# right way to be wrong here — merging two genuinely different stories loses
# one silently, while a missed repeat is visible and fixable.
SIMILARITY_THRESHOLD = 0.40


def _fingerprint(title: str) -> frozenset[str]:
    """Content words of a headline, for comparing two of them.

    NFKC first, because social posts are full of styled Unicode — a post
    written in mathematical italics is the same words as one written plainly,
    and without normalisation they share no characters at all.
    """
    folded = unicodedata.normalize("NFKC", title).lower()
    words = re.findall(r"[a-z0-9]+", folded)
    return frozenset(w for w in words if len(w) > 2 and w not in _STOPWORDS)


def collapse_similar(items: list[Item], *, threshold: float = SIMILARITY_THRESHOLD) -> list[Item]:
    """Merge items whose headlines describe the same story.

    Social accounts covering one announcement produce near-identical posts —
    a single new seahorse species arrived six times in one issue, from two
    outlets. This keeps the first of each group, which after scoring is the
    highest-relevance one, and records how many others said the same thing.

    This is a stopgap for the real clustering pass: it only compares headlines,
    so it catches the obvious repeats and misses coverage worded differently.
    Nothing merged is published, so nothing merged needs crediting.
    """
    kept: list[Item] = []
    prints: list[frozenset[str]] = []
    merged: dict[int, int] = {}

    for item in items:
        marks = _fingerprint(item.title)
        if len(marks) < 3:  # too short to judge; keep it rather than guess
            kept.append(item)
            prints.append(marks)
            continue

        for index, seen in enumerate(prints):
            if not seen:
                continue
            overlap = len(marks & seen) / min(len(marks), len(seen))
            if overlap >= threshold:
                merged[index] = merged.get(index, 0) + 1
                log.info("merged %r into %r", item.title[:60], kept[index].title[:60])
                break
        else:
            kept.append(item)
            prints.append(marks)

    return [
        i.model_copy(update={"extra": {**i.extra, "similar": str(merged[n])}}) if n in merged else i
        for n, i in enumerate(kept)
    ]


def dedupe(items: list[Item]) -> list[Item]:
    """Collapse the same story arriving from several feeds.

    v0 matches on canonical URL only. Clustering genuinely distinct coverage of
    one story is v2's job and needs a model.
    """
    seen: set[str] = set()
    unique: list[Item] = []
    for item in sorted(items, key=lambda i: i.published_at, reverse=True):
        if item.uid in seen:
            continue
        seen.add(item.uid)
        unique.append(item)
    return unique


# How far back an item can be published and still belong in this week's issue.
# Feeds carry whatever the publisher last posted, which on a low-volume site
# can be months old — an early run filed a March press release and two June
# species descriptions as news. From the second run onward the archive
# suppresses anything already seen, so this window mostly governs the first run
# against a new source.
#
# One week, matching the publishing cadence: at weekly, anything older than the
# window has already had its chance to appear and been passed over.
DEFAULT_MAX_AGE_DAYS = 7


def recent(
    items: list[Item],
    *,
    days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> list[Item]:
    """Drop items published longer ago than `days`. `days <= 0` keeps everything."""
    if days <= 0:
        return items
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    fresh = [i for i in items if i.published_at >= cutoff]
    if dropped := len(items) - len(fresh):
        log.info("dropped %d item(s) published more than %d days ago", dropped, days)
    return fresh


def volume_report(items: list[Item], *, now: datetime | None = None) -> str:
    """How much each outlet actually publishes, over 7 / 14 / 30 days.

    The daily-vs-weekly question is a question about publishing volume, and
    volume is measurable rather than arguable. Run this against a full fetch
    and the answer is in the last column: if the six-source total for 7 days
    is smaller than a readable issue, the sources are the problem, not the
    format — and if it's still small at 30 days, the format is.
    """
    now = now or datetime.now(UTC)
    windows = (7, 14, 30)
    by_source: dict[str, list[Item]] = {}
    for i in items:
        by_source.setdefault(i.source_name, []).append(i)

    rows = [f"{'outlet':28} {'7d':>4} {'14d':>4} {'30d':>4}  newest"]
    for name in sorted(by_source, key=lambda n: -len(by_source[n])):
        members = by_source[name]
        counts = [sum(1 for i in members if (now - i.published_at).days <= w) for w in windows]
        newest = max(i.published_at for i in members)
        age = (now - newest).days
        rows.append(
            f"{name[:28]:28} {counts[0]:>4} {counts[1]:>4} {counts[2]:>4}"
            f"  {age}d ago"
        )
    totals = [sum(1 for i in items if (now - i.published_at).days <= w) for w in windows]
    rows.append(f"{'ALL':28} {totals[0]:>4} {totals[1]:>4} {totals[2]:>4}")
    return "\n".join(rows)


# Keeps the closed-enum promise visible at the seam where v1's scoring pass
# will start assigning categories for real.
def categories_in(items: list[Item]) -> list[Category]:
    return sorted({i.category_hint for i in items if i.category_hint}, key=lambda c: list(Category).index(c))
