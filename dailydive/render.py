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


def render_issue(issue: Issue) -> str:
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
    )


def write_issue(issue: Issue, out_dir: Path) -> Path:
    """Write the issue to out_dir/index.html and a dated permalink."""
    html = render_issue(issue)
    out_dir.mkdir(parents=True, exist_ok=True)

    dated = out_dir / "issues" / f"{issue.date:%Y-%m-%d}.html"
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_text(html, encoding="utf-8")

    index = out_dir / "index.html"
    index.write_text(html, encoding="utf-8")
    return index
