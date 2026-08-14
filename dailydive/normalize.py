"""Feed dialects in, one `Item` model out.

Every outlet's quirks get contained here so nothing downstream has to know
whether a thing came from XenForo or YouTube. If a feed entry can't produce a
creditable Item, it is dropped with a warning rather than published half-cited.
"""

from __future__ import annotations

import json
import logging
import re
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
    for stop in (". ", "! ", "? ", "\n"):
        head, sep, _ = plain.partition(stop)
        if sep and len(head) <= limit:
            return head.strip() or None

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


def normalize(source: Source, body: bytes) -> list[Item]:
    """Parse one feed body into Items, dropping anything uncreditable."""
    if source.type is SourceType.YOUTUBE_API:
        return _normalize_youtube_api(source, body)

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


# How far back an item can be published and still count as today's news. Feeds
# carry whatever the publisher last posted, which on a low-volume site can be
# months old — the first live run filed a March press release and two June
# species descriptions as news. From the second run onward the archive
# suppresses anything already seen, so this window mostly governs the first run
# against a new source. Two weeks is generous enough for a genuinely slow
# publisher without letting a quiet feed pad the issue.
DEFAULT_MAX_AGE_DAYS = 14


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
