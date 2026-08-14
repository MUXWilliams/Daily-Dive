"""Core data model.

The attribution invariants live here rather than in a prompt or a template,
because prompts drift and type systems don't. An `Item` cannot be constructed
without the fields needed to credit it, and `assert_attributable` re-checks at
render time so a bug upstream surfaces as an exception instead of an
uncredited line on the published page.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Category(StrEnum):
    """Closed set. An item that fits none of these is dropped rather than
    inventing a new bucket — see README."""

    INDUSTRY = "Industry & Products"
    HUSBANDRY = "Husbandry & Science"
    COMMUNITY = "Community"
    VIDEO = "Video"
    LIVESTOCK = "Livestock & Corals"
    WILD_REEFS = "Wild Reefs"
    EVENTS = "Events"

    @property
    def slug(self) -> str:
        """CSS-safe identifier. Each category owns a colour in the stylesheet,
        keyed off this — so renaming a member renames its class too, and a
        category can never end up styled as a different one."""
        return self.name.lower().replace("_", "-")


class SourceType(StrEnum):
    """Feed dialect. Picks the normalizer, not the fetcher."""

    WORDPRESS = "wordpress"
    XENFORO = "xenforo"
    YOUTUBE = "youtube"
    # The Data API rather than the per-channel RSS feed: JSON, needs a key,
    # and — unlike the RSS path, which robots.txt disallows — it is access
    # YouTube actually sanctions. See Source.is_authorized_api.
    YOUTUBE_API = "youtube_api"
    REDDIT = "reddit"
    GENERIC = "generic"


class AttributionError(ValueError):
    """Raised when something would be published without proper credit."""


# Query params that identify the referrer rather than the resource. Stripped
# before hashing so the same article arriving via three feeds dedupes to one.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "source",
    }
)


def canonicalize_url(url: str) -> str:
    """Normalize a URL for identity comparison.

    Lowercases the host, drops the fragment and tracking params, and removes a
    trailing slash on the path. Deliberately conservative: it never touches the
    scheme's host path semantics beyond case, because some forums are
    path-case-sensitive.
    """
    parts = urlsplit(url.strip())
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


class Source(BaseModel):
    """One feed. These come from sources.toml — the file you edit most."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    url: str
    type: SourceType = SourceType.GENERIC
    category_hint: Category | None = None
    # Forum/subreddit/channel display name, when the outlet name alone is too
    # coarse to credit properly ("Reef2Reef" vs "Reef2Reef — Reef Chemistry").
    section: str | None = None
    enabled: bool = True

    @property
    def display_name(self) -> str:
        return f"{self.name} — {self.section}" if self.section else self.name

    @property
    def is_authorized_api(self) -> bool:
        """True when access is granted by an API key and its terms of service.

        robots.txt governs crawlers discovering and fetching pages. It is not
        the mechanism by which a provider grants programmatic access — that is
        the API, its key, and its quota. YouTube disallows /feeds/ in
        robots.txt while simultaneously operating a public Data API for
        exactly this purpose, so honoring robots.txt on an authenticated API
        call would be obeying the letter of a rule that was never addressed
        to us, and declining an invitation the provider explicitly extended.

        This deliberately does NOT open a general escape hatch: it is a
        property of the source type, not a per-source flag, so no ordinary
        feed can quietly opt out of the robots check by editing sources.toml.
        """
        return self.type is SourceType.YOUTUBE_API


class Item(BaseModel):
    """A single thing someone published. Immutable once built."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    source_name: str
    title: str
    url: str
    published_at: datetime
    author: str | None = None
    # Verbatim text from the feed. Kept for the scoring/summarizing passes in
    # v1+; never rendered directly, so no excerpt-length policy applies yet.
    raw_text: str | None = None
    category_hint: Category | None = None
    extra: dict[str, str] = Field(default_factory=dict)

    @field_validator("title", "source_name", "source_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise AttributionError("title, source_name and source_id must be non-empty")
        return v.strip()

    @field_validator("url")
    @classmethod
    def _resolvable(cls, v: str) -> str:
        parts = urlsplit(v.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise AttributionError(f"item url must be an absolute http(s) URL, got {v!r}")
        return v.strip()

    @property
    def canonical_url(self) -> str:
        return canonicalize_url(self.url)

    @property
    def uid(self) -> str:
        """Stable identity for dedupe and for the seen-items table."""
        return hashlib.sha256(self.canonical_url.encode()).hexdigest()[:16]


def assert_attributable(item: Item) -> None:
    """Belt-and-braces check at the render boundary.

    Pydantic already enforces these at construction, so reaching this with a
    bad item means something bypassed the model. Fail loudly rather than
    publish an uncredited line.
    """
    if not item.source_name.strip():
        raise AttributionError(f"{item.uid}: missing source_name")
    if not item.title.strip():
        raise AttributionError(f"{item.uid}: missing title")
    parts = urlsplit(item.url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise AttributionError(f"{item.uid}: unresolvable url {item.url!r}")


class Issue(BaseModel):
    """One morning's output. Render targets all read from this."""

    model_config = ConfigDict(frozen=True)

    date: datetime
    items: list[Item]

    @property
    def outlets(self) -> list[str]:
        """Every outlet that appears, for the credit footer."""
        return sorted({i.source_name for i in self.items})
