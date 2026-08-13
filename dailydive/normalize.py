"""Feed dialects in, one `Item` model out.

Every outlet's quirks get contained here so nothing downstream has to know
whether a thing came from XenForo or YouTube. If a feed entry can't produce a
creditable Item, it is dropped with a warning rather than published half-cited.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

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


def _reddit_extra(entry: feedparser.FeedParserDict) -> dict[str, str]:
    return {"subreddit": entry["source"]["title"]} if isinstance(entry.get("source"), dict) else {}


def normalize(source: Source, body: bytes) -> list[Item]:
    """Parse one feed body into Items, dropping anything uncreditable."""
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        log.warning("%s: unparseable feed (%s)", source.id, parsed.get("bozo_exception"))
        return []

    items: list[Item] = []
    for entry in parsed.entries:
        url = entry.get("link")
        title = entry.get("title")
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
                    raw_text=_text(entry.get("summary") or entry.get("description")),
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


# Keeps the closed-enum promise visible at the seam where v1's scoring pass
# will start assigning categories for real.
def categories_in(items: list[Item]) -> list[Category]:
    return sorted({i.category_hint for i in items if i.category_hint}, key=lambda c: list(Category).index(c))
