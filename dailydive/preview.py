"""Render the template against a frozen issue, for design work.

Iterating on layout used to mean one of two bad options: wait for Friday, or
spend a scoring pass to rebuild a real issue. Neither is a design loop. This is
the third option — no network, no model calls, no cost, and the same output
every time, so a change to the stylesheet is the only thing that moves.

The fixture is real: 24 items lifted from the first published issue, with their
real headlines, outlets and gists. Two synthetic items are appended and clearly
labelled, because the real issue happened to carry no Events and nothing
uncategorized, and those sections need to render too. Categories are spread
across the full set for the same reason — this is design coverage, not an
editorial judgement about where those stories belong.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from datetime import UTC, datetime
from pathlib import Path

from .models import Category, Issue, Item

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "preview-issue.json"

# A Saturday, matching the first published issue, so the greeting reads
# naturally and the date formatting is exercised.
PREVIEW_DATE = datetime(2026, 8, 15, tzinfo=UTC)


def load_issue(fixture: Path = FIXTURE) -> Issue:
    """The frozen issue, as an Issue the renderer will accept."""
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    items: list[Item] = []
    for n, entry in enumerate(raw):
        extra: dict[str, str] = {}
        if entry.get("gist"):
            extra["gist"] = entry["gist"]
        # Badges that only some items carry. Included so a layout change is
        # judged against the messy case rather than the tidy one.
        for key in ("duration_s", "beat", "similar"):
            if entry.get(key) is not None:
                extra[key] = str(entry[key])
        # Descending, so the ordering matches a scored issue.
        extra["relevance"] = f"{max(0.45, 0.95 - n * 0.02):.2f}"

        items.append(
            Item(
                source_id="preview",
                source_name=entry["source_name"],
                title=entry["title"],
                url=entry["url"],
                published_at=datetime.fromisoformat(entry["published_at"]),
                author=entry.get("author"),
                category_hint=Category(entry["category"]) if entry.get("category") else None,
                extra=extra,
            )
        )
    return Issue(date=PREVIEW_DATE, items=items)


def inline_assets(html: str, site_dir: Path) -> str:
    """Embed referenced local assets as data URIs.

    A preview gets looked at on a phone, in a chat window, anywhere but the
    directory it was written to — and a masthead that resolves to a missing
    file makes the page look broken in a way the design is not. Inlining costs
    a larger file and buys a page that renders correctly anywhere.
    """
    for asset in sorted(site_dir.glob("assets/*")):
        ref = f"assets/{asset.name}"
        if ref not in html:
            continue
        mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
        data = base64.b64encode(asset.read_bytes()).decode()
        html = html.replace(f'src="{ref}"', f'src="data:{mime};base64,{data}"')
        # The absolute og:image URL points at the live site and must not be
        # rewritten to a data URI — no unfurler would accept one.
    return html


def write_preview(out_dir: Path, *, site_dir: Path = Path("site"), standalone: bool = True) -> Path:
    """Render the fixture to out_dir/index.html. Returns the path written."""
    from . import render  # local import keeps preview off the hot path

    out_dir.mkdir(parents=True, exist_ok=True)
    issue = load_issue()
    # Same archive link a real front page carries. Without it the staging page
    # is missing a piece of chrome the live page has, which is the one thing a
    # preview must never be.
    html = render.render_issue(
        issue, header=render.find_header_image(site_dir), archive_href="archive.html"
    )
    if standalone:
        html = inline_assets(html, site_dir)
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


# The staging page is the design under review, so it gets no harness. Chrome
# around it — device frames, a toolbar, a "staging" banner — would compete with
# the exact thing being judged. What ships is the page, at a stable URL, on
# whatever screen you open it on.
ARTIFACT_TITLE = "Weekly Dive"


def artifact_html(out_dir: Path, *, site_dir: Path = Path("site")) -> str:
    """The preview reshaped for a hosted artifact.

    An artifact supplies its own doctype, head and body, so a full HTML
    document cannot be published as-is. This lifts the stylesheet and the body
    content out of the rendered page and leaves the rest behind — the canonical
    and OpenGraph tags in particular, which name the live site and would be
    wrong here.

    Regenerated from the same fixture and the same template as the local
    preview, so the staging page and the real page cannot drift.
    """
    write_preview(out_dir, site_dir=site_dir, standalone=True)
    doc = (out_dir / "index.html").read_text(encoding="utf-8")

    styles = re.findall(r"<style>.*?</style>", doc, re.S)
    if not styles:
        raise RuntimeError("no <style> block found — did the template change shape?")
    body = re.search(r"<body>(.*)</body>", doc, re.S)
    if body is None:
        raise RuntimeError("no <body> found — did the template change shape?")

    return f"<title>{ARTIFACT_TITLE}</title>\n" + "\n".join(styles) + "\n" + body.group(1).strip() + "\n"
