"""Issue -> HTML.

Every item passes `assert_attributable` before it reaches a template, so a
missing credit is an exception at build time rather than an uncredited line on
a public page.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import Category, Issue, assert_attributable

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.removeprefix("www.")


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
        case "stamp":  # 2026-08-13 09:31 UTC
            return f"{dt:%Y-%m-%d %H:%M} {dt.tzname() or ''}".strip()
        case _:  # August 13, 2026
            return f"{dt:%B} {dt.day}, {dt.year}"


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


# Drop a banner here and the page uses it; leave it out and the page draws its
# own. Either way the masthead renders — the artwork is an upgrade, never a
# dependency, so a missing file can't produce a broken header in production.
HEADER_IMAGE = "assets/dailydive-header.png"


def find_header_image(out_dir: Path, *, depth: int = 0) -> str | None:
    """Relative path to the banner, or None if it isn't there.

    Relative rather than absolute so the page renders correctly both on the
    live site and when opened straight off disk — `depth` is how many
    directories down from the site root the page sits, so the dated permalink
    under issues/ gets the `../` it needs.
    """
    if not (out_dir / HEADER_IMAGE).is_file():
        return None
    return ("../" * depth) + HEADER_IMAGE


def render_issue(issue: Issue, *, header_image: str | None = None) -> str:
    for item in issue.items:
        assert_attributable(item)

    env = _env()
    env.filters["host"] = _host
    env.filters["datefmt"] = _datefmt
    template = env.get_template("issue.html.j2")
    return template.render(
        issue=issue,
        sections=group_by_category(issue),
        generated_at=datetime.now(issue.date.tzinfo),
        header_image=header_image,
    )


def write_issue(issue: Issue, out_dir: Path) -> Path:
    """Write the issue to out_dir/index.html and a dated permalink.

    Rendered twice, because the two pages sit at different depths and the
    banner path has to resolve from each of them.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    dated = out_dir / "issues" / f"{issue.date:%Y-%m-%d}.html"
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_text(
        render_issue(issue, header_image=find_header_image(out_dir, depth=1)),
        encoding="utf-8",
    )

    index = out_dir / "index.html"
    index.write_text(
        render_issue(issue, header_image=find_header_image(out_dir)),
        encoding="utf-8",
    )
    return index
