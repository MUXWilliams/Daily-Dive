"""Issue -> HTML.

Every item passes `assert_attributable` before it reaches a template, so a
missing credit is an exception at build time rather than an uncredited line on
a public page.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import brand
from .models import Category, Issue, assert_attributable

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        # select_autoescape matches on the filename's final extension, and the
        # template is issue.html.j2 — so ["html"] never matched and every feed
        # title was being written into the page unescaped. Titles are attacker-
        # controlled in the ordinary case: anyone who can post to a syndicated
        # forum or blog can put markup in one. `default=True` is the belt to
        # the extension list's braces, so renaming a template can't silently
        # turn escaping off again.
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml", "j2"),
            default_for_string=True,
            default=True,
        ),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.removeprefix("www.")


def highlights(issue: Issue, limit: int = 4) -> tuple[list[str], str | None]:
    """The morning's headline bullets, plus a line covering the remainder.

    Derived from the ranked items rather than written separately, so the intro
    can never advertise a story the issue doesn't contain. Items arrive sorted
    by relevance from the scoring pass; without scores this falls back to
    document order, which is the source feeds' own ordering.
    """
    if not issue.items:
        return [], None

    top = issue.items[:limit]
    rest = issue.items[limit:]

    plus = None
    if rest:
        areas = []
        for item in rest:
            name = item.category_hint.value if item.category_hint else "more"
            if name not in areas:
                areas.append(name)
        listed = ", ".join(a.lower() for a in areas[:3])
        plus = f"Plus {len(rest)} more across {listed}"

    return [i.title for i in top], plus


def _datefmt(dt: datetime, style: str = "long") -> str:
    """Format a date without platform-specific strftime codes.

    `%-d` (no zero padding) is glibc-only and raises on Windows, so the day
    number is interpolated directly instead.
    """
    match style:
        case "full":  # Thursday, August 13, 2026
            return f"{dt:%A}, {dt:%B} {dt.day}, {dt.year}"
        case "short":  # Aug 13
            return f"{dt:%b} {dt.day}"
        case "weekday":  # Thursday
            return f"{dt:%A}"
        case "stamp":  # 2026-08-13 09:31 UTC
            return f"{dt:%Y-%m-%d %H:%M} {dt.tzname() or ''}".strip()
        case _:  # August 13, 2026
            return f"{dt:%B} {dt.day}, {dt.year}"


def _runtime(seconds: str | int) -> str:
    """Seconds -> "14 min" / "1h 22m". Shown so a reader can judge the ask.

    Nothing under a minute can appear here — Shorts are filtered upstream —
    so the shortest label this ever produces is "4 min".
    """
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    hours, minutes = divmod(round(total / 60), 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes} min"


def group_by_category(issue: Issue, *, exclude: object = None) -> list[tuple[str, str, list]]:
    """Section the issue, in the canonical category order, skipping empties.

    Returns (title, slug, items). The slug keys the section's colour in the
    stylesheet — one hue per category, carried through the heading, the source
    name, and the link hover, so the colour tells the reader where they are
    before they read a word.

    `exclude` drops a single item, which is how the Resource video avoids
    appearing twice: once in its own section at the foot of the page and again
    under Community. Compared by identity, not equality — two items can be
    equal-by-value and only one of them is the one being promoted.
    """
    kept = [i for i in issue.items if i is not exclude]

    buckets: list[tuple[str, str, list]] = []
    for category in Category:
        members = [i for i in kept if i.category_hint is category]
        if members:
            buckets.append((category.value, category.slug, members))

    uncategorized = [i for i in kept if i.category_hint is None]
    if uncategorized:
        buckets.append(("Elsewhere", "elsewhere", uncategorized))
    return buckets


# Titles that announce instruction rather than news. The section is called
# Resource, so what fills it should be something you'd come back to, not
# whichever video happened to score highest that week.
#
# Word boundaries and explicit stems, not a substring test: `"tip" in title`
# files "multiple" and "multiples" under tips, and a bare "mistake" stem would
# eventually be shortened by someone into one that catches "misuse". Adding a
# word here is a one-line edit and is meant to be.
RESOURCE_WORDS = re.compile(r"\b(how[\s-]?to|tips?|tricks?|resources?|mistakes?)\b", re.I)


def _relevance(item) -> float:
    """The score the model gave, or 0 for an item that never went through it."""
    try:
        return float(item.extra.get("relevance", 0))
    except (TypeError, ValueError):
        return 0.0


def pick_resource(issue: Issue):
    """The one video promoted to the Resource section, or None.

    Order of preference:

    1. An editor's pick. Picks outrank the model everywhere else in this
       pipeline — a video chosen by hand should not lose the slot to a keyword.
    2. The highest-scoring video whose title reads as instructional.
    3. The highest-scoring video, so a week with no how-to still fills the slot.
       An empty section in a fixed layout reads as a bug to a reader who does
       not know the rule.

    Videos are identified by `extra["video_id"]`, which normalize.py sets for
    both the RSS and Data API paths — so this needs no network and no quota.
    """
    videos = [i for i in issue.items if i.extra.get("video_id")]
    if not videos:
        return None

    picks = [i for i in videos if i.extra.get("pick")]
    if picks:
        return max(picks, key=_relevance)

    instructional = [i for i in videos if RESOURCE_WORDS.search(i.title)]
    return max(instructional or videos, key=_relevance)


# Drop a banner in assets/ and the page uses it; leave it out and the page
# draws its own. Either way the masthead renders — the artwork is an upgrade,
# never a dependency, so a missing file cannot produce a broken header.
#
# Several names are accepted, current convention first, so replacing the
# artwork does not mean editing code. "masthead" rather than a filename with
# the publication in it: the publication can be renamed and the file should not
# have to be.
HEADER_CANDIDATES = (
    "assets/masthead.png",
    "assets/masthead.webp",
    "assets/masthead.jpg",
    "assets/dailydive-header.png",  # the original name, kept working
)


def _png_size(path: Path) -> tuple[int, int] | None:
    """Pixel dimensions from a PNG header, or None if unreadable.

    The template needs these to reserve the right space before the image
    loads. They used to be typed into the template by hand, which meant any
    replacement banner of a different size would render at the old aspect
    ratio and jump on load. Reading them makes new artwork a drop-in.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return (width, height) if width and height else None


def find_header_image(
    out_dir: Path, *, depth: int = 0
) -> tuple[str, int | None, int | None] | None:
    """Relative path to the banner and its size, or None if there isn't one.

    Relative rather than absolute so the page renders correctly both on the
    live site and when opened straight off disk — `depth` is how many
    directories down from the site root the page sits, so the dated permalink
    under issues/ gets the `../` it needs.

    Dimensions come back None for formats this cannot measure, and the template
    omits the attributes: CSS still sizes the image, only the layout
    reservation is lost.
    """
    for name in HEADER_CANDIDATES:
        path = out_dir / name
        if not path.is_file():
            continue
        size = _png_size(path) if path.suffix == ".png" else None
        width, height = size if size else (None, None)
        return (("../" * depth) + name, width, height)
    return None


def og_description(issue: Issue) -> str:
    """The one line a link preview shows. Concrete beats generic.

    "12 items from 7 outlets, led by ..." tells someone deciding whether to
    open a forwarded link what is actually in it; the standing tagline tells
    them only what the publication is, which they can see from the title.
    """
    if not issue.items:
        return brand.DESCRIPTION
    count = len(issue.items)
    outlets = len(issue.outlets)
    lead = issue.items[0].title
    if len(lead) > 90:
        lead = lead[:89].rstrip() + "\u2026"
    return (
        f"{count} item{'' if count == 1 else 's'} from "
        f"{outlets} outlet{'' if outlets == 1 else 's'}, led by \u201c{lead}\u201d"
    )


def render_issue(
    issue: Issue,
    *,
    header: tuple[str, int | None, int | None] | None = None,
    canonical_path: str = "",
    archive_href: str = "",
    thumb: tuple[str, int | None, int | None] | None = None,
) -> str:
    for item in issue.items:
        assert_attributable(item)

    env = _env()
    env.filters["host"] = _host
    env.filters["datefmt"] = _datefmt
    env.filters["runtime"] = _runtime
    template = env.get_template("issue.html.j2")
    bullets, plus = highlights(issue)
    resource = pick_resource(issue)
    return template.render(
        issue=issue,
        sections=group_by_category(issue, exclude=resource),
        resource=resource,
        # The image is optional even when the video isn't: a thumbnail that
        # failed to fetch costs the picture, not the section.
        thumb=thumb,
        generated_at=datetime.now(issue.date.tzinfo),
        header_image=header[0] if header else None,
        header_width=header[1] if header else None,
        header_height=header[2] if header else None,
        brand=brand,
        highlights=bullets,
        highlights_plus=plus,
        canonical_path=canonical_path,
        archive_href=archive_href,
        og_description=og_description(issue),
    )


# Acronyms worth preserving when a shouting headline is re-cased. Title-casing
# turns "PAM" into "Pam", and these are terms the reader knows by their capitals.
# Short and domain-specific on purpose: a long list starts mangling ordinary
# words that happen to match.
ACRONYMS = frozenset({
    "PAM", "RGB", "DNA", "RNA", "LED", "SPS", "LPS", "NOAA", "ENSO", "UV",
    "ICP", "MPA", "MPAS", "PAR", "GFO", "ATS", "CITES", "USA", "US", "UK",
    "AI", "API", "PH", "CO2", "SST", "ROV", "OA",
})

# Words that stay lowercase inside a headline unless they lead it.
_MINOR = frozenset({
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of",
    "on", "or", "the", "to", "vs", "via", "with", "from", "into",
})


def _unshout(title: str) -> str:
    """Re-case a headline that arrived in capitals.

    OpenAlex and some journals hand back titles in full caps. Passed straight
    into a subject line that is two problems at once: all-caps is a long-
    standing spam-filter signal, and it reads as shouting at a reader who did
    not ask to be shouted at.

    Applied ONLY to the subject line, never to the title shown on the page or
    in the email body. Those are the outlet's own words and this project
    reproduces them as given — normalising a headline in place would be editing
    someone else's copy under their byline. A subject line is ours to compose.
    """
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 12:
        return title
    upper_share = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper_share < 0.7:
        return title

    words = title.split()
    out: list[str] = []
    for i, word in enumerate(words):
        stripped = word.strip(".,:;!?()[]\"'")
        if stripped.upper() in ACRONYMS:
            out.append(word.upper())
        elif i and stripped.lower() in _MINOR:
            out.append(word.lower())
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def subject(issue: Issue) -> str:
    """The subject line.

    Concrete beats standing. Naming the lead story tells someone scanning an
    inbox what is actually inside; a date tells them only what they already
    subscribed to. Same reasoning as og_description, which solved the same
    problem for link previews.
    """
    lead = _unshout(issue.items[0].title) if issue.items else ""
    if len(lead) > 70:
        lead = lead[:69].rstrip() + "\u2026"
    if not lead:
        return f"{brand.PUBLICATION} — {_datefmt(issue.date, 'full')}"
    return f"{brand.PUBLICATION} — {lead}"


def render_email(
    issue: Issue, *, thumb_url: str | None = None
) -> str:
    """The issue as email-safe HTML.

    A separate template rather than a variant of the page. `issue.html.j2` is
    built on CSS custom properties, color-mix() and position:sticky; none
    survive Outlook and the first two do not survive Gmail, so reusing it would
    produce something that previews correctly in a browser and arrives
    unstyled.

    `thumb_url` is absolute and optional. Relative paths mean nothing in an
    inbox, and a missing image costs the picture rather than the section.
    """
    for item in issue.items:
        assert_attributable(item)

    env = _env()
    env.filters["host"] = _host
    env.filters["datefmt"] = _datefmt
    env.filters["runtime"] = _runtime
    bullets, plus = highlights(issue)
    resource = pick_resource(issue)
    return env.get_template("email.html.j2").render(
        issue=issue,
        sections=group_by_category(issue, exclude=resource),
        resource=resource,
        thumb=thumb_url,
        brand=brand,
        highlights=bullets,
        highlights_plus=plus,
        preheader=og_description(issue),
    )


def as_text(issue: Issue) -> str:
    """The issue as plain text, for reading in a terminal or a CI log.

    The built page ships as a CI artifact, which is awkward to open and, on
    some networks, awkward even to download — so editorial review shouldn't
    depend on it. This is what you read to judge the scoring pass: what it
    kept, where it filed it, how confident it was, and whether the gist says
    anything the headline didn't.
    """
    lines = [f"{brand.PUBLICATION} — {_datefmt(issue.date, 'full')} — {len(issue.items)} items"]
    # Same sectioning as the page, Resource included, so reviewing a run in a CI
    # log tells you where things actually landed.
    resource = pick_resource(issue)
    sections = group_by_category(issue, exclude=resource)
    if resource is not None:
        sections = [*sections, ("Resource", "resource", [resource])]
    for title, _slug, members in sections:
        lines += ["", f"## {title} ({len(members)})"]
        for item in members:
            score = item.extra.get("relevance")
            beat = item.extra.get("beat")
            prefix = f"  [{score}] " if score else "  "
            run = _runtime(item.extra["duration_s"]) if item.extra.get("duration_s") else ""
            also = f" +{item.extra['similar']} similar" if item.extra.get("similar") else ""
            lines.append(
                f"{prefix}{f'({beat}) ' if beat else ''}{item.title}"
                f"{f' [{run}]' if run else ''}{also}"
            )
            if item.extra.get("gist"):
                lines.append(f"      {item.extra['gist']}")
            # The byline belongs here too. The HTML credit line carries it and
            # the plain-text form dropped it, which mattered the first time a
            # source's configured name had to be checked against what the
            # publisher actually calls itself — the API knows, and this is
            # where a run says so.
            by = f" · {item.author}" if item.author else ""
            lines.append(f"      — {item.source_name}{by} · {item.url}")
    return "\n".join(lines)


def write_about(out_dir: Path) -> Path:
    """Write the about & sourcing policy page.

    Rendered rather than hand-written. It was hand-written for most of this
    project's life, which is exactly why it spent weeks telling readers the
    publication was called Daily Dive and went out every morning: nothing tied
    the words on it to `brand.py`. Every other page on the site renders from a
    template and none of them drifted.

    Takes no Issue — the page says nothing about today — so it is safe to call
    on any run that writes the site.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    html = _env().get_template("about.html.j2").render(
        brand=brand,
        header=find_header_image(out_dir),
    )
    path = out_dir / "about.html"
    path.write_text(html, encoding="utf-8")
    return path


def write_subscribe(out_dir: Path) -> Path:
    """Write the subscribe redirect.

    The signup form lives with the mailing-list provider because a static site
    cannot accept a form POST — that is the one thing this project pays anyone
    for. This page exists so the *shareable* URL is still ours: switching
    providers means editing one file, not chasing every forum post and chat
    message the old link reached.
    """
    from . import deliver

    out_dir.mkdir(parents=True, exist_ok=True)
    html = _env().get_template("subscribe.html.j2").render(
        brand=brand, subscribe_url=deliver.SUBSCRIBE_URL
    )
    path = out_dir / "subscribe.html"
    path.write_text(html, encoding="utf-8")
    return path


def write_issue(issue: Issue, out_dir: Path) -> Path:
    """Write the issue to out_dir/index.html and a dated permalink.

    Rendered twice, because the two pages sit at different depths and the
    banner path has to resolve from each of them.

    The Resource thumbnail is read from disk, never fetched here — a render must
    stay offline so `daily-dive preview` and the tests keep working. Whatever
    put the file there (`cli`, on a publishing run) is what talks to the
    network; if nothing did, the section renders without its picture.
    """
    from . import thumbs

    out_dir.mkdir(parents=True, exist_ok=True)
    resource = pick_resource(issue)
    video_id = resource.extra.get("video_id") if resource else None

    dated = out_dir / "issues" / f"{issue.date:%Y-%m-%d}.html"
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_text(
        render_issue(
            issue,
            header=find_header_image(out_dir, depth=1),
            canonical_path=f"issues/{issue.date:%Y-%m-%d}.html",
            archive_href="../archive.html",
            thumb=thumbs.existing(video_id, out_dir, depth=1) if video_id else None,
        ),
        encoding="utf-8",
    )

    index = out_dir / "index.html"
    index.write_text(
        render_issue(
            issue,
            header=find_header_image(out_dir),
            archive_href="archive.html",
            thumb=thumbs.existing(video_id, out_dir) if video_id else None,
        ),
        encoding="utf-8",
    )
    return index
