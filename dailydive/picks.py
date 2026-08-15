"""Hand-picked stories, filed as GitHub issues.

The crawler is locked out of places worth reading. Reef2Reef and Humble.Fish
both 403 every feed shape; product pages and trade sites often publish no feed
at all. Forging a User-Agent would get past the first and does not exist for
the second, and either way it is the thing this project has refused to do.

A person reading those sites and citing what they found is not a crawler. This
module is that door: the editor files a story as an issue during the week, and
the Friday build drains the bucket.

Design notes worth keeping:

- **The bucket is open issues.** Closing one is how it leaves the bucket, so
  "not yet published" needs no extra state anywhere.
- **The repo is public**, which is what makes Actions and Pages free — and
  means anyone can open an issue on it. Only issues opened by an allowlisted
  account become items. Same posture as the IMAP sender allowlist, for the same
  reason: an issue tracker looks like a private inbox and is not one.
- **There is no author field**, deliberately. Forum members did not ask to be
  published, and a privacy mistake is not undoable once it is on a public page
  and in git history. A pick credits the site.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx

from .models import Category, Item

log = logging.getLogger(__name__)

# Accounts whose issues may become items. Explicit rather than derived from the
# repo owner, so widening it is a reviewable diff rather than a config change
# nobody sees. Compared case-insensitively — GitHub logins are.
AUTHORS: frozenset[str] = frozenset({"muxwilliams"})

LABEL = "pick"
API = "https://api.github.com"

# Matches what the pick sheet emits and what a GitHub issue form produces:
# a "### Field" heading, then the value until the next heading.
_SECTION = re.compile(r"^###\s+(?P<label>.+?)\s*$\n+(?P<value>.*?)(?=^###\s|\Z)", re.M | re.S)

# GitHub writes this into an issue-form field the user left blank.
_BLANK = "_no response_"

# Mirrors score.GIST_MAX_WORDS. Duplicated as a constant rather than imported
# so this module does not pull in the scoring stack for one integer.
GIST_MAX_WORDS = 40


class PickError(ValueError):
    """A pick that cannot become an item, with a reason fit to show the editor."""


def parse_body(body: str) -> dict[str, str]:
    """The issue body's "### Field / value" sections, keyed by lowercased label."""
    fields: dict[str, str] = {}
    for match in _SECTION.finditer(body or ""):
        value = match.group("value").strip()
        if value.lower() == _BLANK:
            value = ""
        fields[match.group("label").strip().lower()] = value
    return fields


def _category(raw: str) -> Category:
    for member in Category:
        if member.value.lower() == raw.lower():
            return member
    known = ", ".join(m.value for m in Category)
    raise PickError(f"I don't recognise the category {raw!r}. It has to be one of: {known}.")


def _published(raw: str) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError:
        raise PickError(
            f"I couldn't read {raw!r} as a date. Use YYYY-MM-DD, or leave it blank for today."
        ) from None


def to_item(body: str, *, number: int | None = None) -> Item:
    """One issue body -> one Item. Raises PickError with a readable reason.

    Deliberately strict. A pick reaches the page without the scorer ever seeing
    it, so this is the only gate it passes through — and the attribution rules
    do not relax for the editor.
    """
    fields = parse_body(body)

    headline = fields.get("headline", "")
    link = fields.get("link", "")
    outlet = fields.get("outlet", "")

    missing = [
        name
        for name, value in (("a headline", headline), ("a link", link), ("an outlet", outlet))
        if not value
    ]
    if missing:
        raise PickError(f"This is missing {', '.join(missing)}.")

    if not re.match(r"^https?://\S+$", link):
        raise PickError(f"{link!r} isn't a usable link — it needs to start with http:// or https://.")

    gist = fields.get("why it matters", "")
    if len(gist.split()) > GIST_MAX_WORDS:
        raise PickError(
            f"The gist runs to {len(gist.split())} words and the ceiling is {GIST_MAX_WORDS}. "
            "Trim it and reopen this."
        )

    extra: dict[str, str] = {"pick": "1"}
    if number is not None:
        # So the build can close the right issue once this reaches a page.
        extra["pick_issue"] = str(number)
    if gist:
        extra["gist"] = gist
    if beat := fields.get("industry beat", ""):
        extra["beat"] = beat

    return Item(
        source_id="pick",
        source_name=outlet,
        title=headline,
        url=link,
        published_at=_published(fields.get("published", "")),
        # No author, ever. See the module docstring.
        author=None,
        category_hint=_category(fields.get("category", "")),
        extra=extra,
    )


def is_pick(item: Item) -> bool:
    return item.extra.get("pick") == "1"


class Bucket:
    """The open picks, and the means to answer them.

    Thin on purpose: everything that decides anything lives in the functions
    above, where it can be tested without a network.
    """

    def __init__(self, repo: str, token: str, *, client: httpx.Client | None = None) -> None:
        self.repo = repo
        self._client = client or httpx.Client(timeout=20.0)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def open_picks(self) -> list[dict]:
        """Open issues labelled `pick`, from allowlisted authors only.

        An issue from anyone else is ignored in silence. It is not an error and
        it gets no reply — a stranger filing on a public repo should not learn
        anything from how the build responds.
        """
        resp = self._client.get(
            f"{API}/repos/{self.repo}/issues",
            headers=self._headers,
            params={"state": "open", "labels": LABEL, "per_page": 50},
        )
        resp.raise_for_status()
        allowed, ignored = [], 0
        for issue in resp.json():
            if "pull_request" in issue:  # the issues endpoint returns PRs too
                continue
            author = (issue.get("user") or {}).get("login", "")
            if author.lower() in AUTHORS:
                allowed.append(issue)
            else:
                ignored += 1
        if ignored:
            log.warning("ignored %d pick issue(s) from non-allowlisted accounts", ignored)
        return allowed

    def comment(self, number: int, message: str) -> None:
        self._client.post(
            f"{API}/repos/{self.repo}/issues/{number}/comments",
            headers=self._headers,
            json={"body": message},
        ).raise_for_status()

    def close(self, number: int, message: str) -> None:
        self.comment(number, message)
        self._client.patch(
            f"{API}/repos/{self.repo}/issues/{number}",
            headers=self._headers,
            json={"state": "closed", "state_reason": "completed"},
        ).raise_for_status()


def collect(bucket: Bucket, *, published_uids: set[str]) -> tuple[list[Item], list[tuple[int, str]]]:
    """Read the bucket. Returns (items, [(issue number, rejection reason)]).

    A pick for something already published is rejected rather than duplicated —
    the editor asked for that explicitly. Note this checks what was *published*,
    not what was fetched: a pick's URL comes from a source the crawler cannot
    reach, so it could never be in the fetched set anyway.
    """
    items: list[Item] = []
    rejected: list[tuple[int, str]] = []

    for issue in bucket.open_picks():
        number = issue["number"]
        try:
            item = to_item(issue.get("body") or "", number=number)
        except PickError as exc:
            rejected.append((number, str(exc)))
            continue
        if item.uid in published_uids:
            rejected.append((number, "This one already ran in an earlier issue, so I've left it out."))
            continue
        items.append(item)

    return items, rejected
