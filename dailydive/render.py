"""Issue -> HTML.

Every item passes `assert_attributable` before it reaches a template, so a
missing credit is an exception at build time rather than an uncredited line on
a public page.
"""

from __future__ import annotations

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


def group_by_category(issue: Issue) -> list[tuple[str, str, list]]:
    """Section the issue, in the canonical category order, skipping empties.

    Returns (title, slug, items). The slug keys the section's colour in the
    stylesheet — one hue per category, carried through the heading, the source
    name, and the link hover, so the colour tells the reader where they are
    before they read a word.
    """
    buckets: list[tuple[str, str, list]] = []
    for category in Category:
        members = [i for i in issue.items if i.category_hint is category]
        if members:
            buckets.append((category.value, category.slug, members))

    uncategorized = [i for i in issue.items if i.category_hint is None]
    if uncategorized:
        buckets.append(("Elsewhere", "elsewhere", uncategorized))
    return buckets


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
) -> str:
    for item in issue.items:
        assert_attributable(item)

    env = _env()
    env.filters["host"] = _host
    env.filters["datefmt"] = _datefmt
    env.filters["runtime"] = _runtime
    template = env.get_template("issue.html.j2")
    bullets, plus = highlights(issue)
    return template.render(
        issue=issue,
        sections=group_by_category(issue),
        generated_at=datetime.now(issue.date.tzinfo),
        header_image=header[0] if header else None,
        header_width=header[1] if header else None,
        header_height=header[2] if header else None,
        brand=brand,
        highlights=bullets,
        highlights_plus=plus,
        canonical_path=canonical_path,
        og_description=og_description(issue),
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
    for title, _slug, members in group_by_category(issue):
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


def write_issue(issue: Issue, out_dir: Path) -> Path:
    """Write the issue to out_dir/index.html and a dated permalink.

    Rendered twice, because the two pages sit at different depths and the
    banner path has to resolve from each of them.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    dated = out_dir / "issues" / f"{issue.date:%Y-%m-%d}.html"
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_text(
        render_issue(
            issue,
            header=find_header_image(out_dir, depth=1),
            canonical_path=f"issues/{issue.date:%Y-%m-%d}.html",
        ),
        encoding="utf-8",
    )

    index = out_dir / "index.html"
    index.write_text(
        render_issue(issue, header=find_header_image(out_dir)),
        encoding="utf-8",
    )
    return index
