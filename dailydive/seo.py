"""The two files a search engine looks for, and neither of which existed.

The site has been live for weeks with no `robots.txt` and no `sitemap.xml`.
Neither absence blocks Google — a missing robots.txt means "crawl everything",
and a sitemap is a hint rather than a requirement — but together they are the
cheapest thing that can be done for a brand-new domain with almost no inbound
links, which is the actual problem here. A sitemap is how Google learns that
`/issues/2026-08-15.html` exists without having to find a link to it first.

Both are generated, not hand-written, for the same reason `about.html` is:
a hand-maintained list of URLs is a list that silently stops matching the site.
The sitemap is built from `site/issues/index.json`, which `archive.py` already
maintains as the machine-readable record of what has been published — the same
sidecar that will feed the RSS feed. Nothing here parses the site's own HTML.

`lastmod` is only emitted where a real date is known. The issue pages and the
front page have one; `about.html` and `subscribe.html` do not, and inventing a
plausible-looking timestamp for them would be telling a crawler something that
is not true in order to look tidy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from xml.sax.saxutils import escape

from . import archive, brand

log = logging.getLogger(__name__)

ROBOTS = "robots.txt"
SITEMAP = "sitemap.xml"
SITEMAP_URL = f"{brand.SITE_URL}/{SITEMAP}"

# Pages that are always present and are not dated. Order is the order they
# appear in the file, which is meaningless to a crawler and useful to a human
# reading it.
STATIC_PAGES = ("about.html", "subscribe.html")

# Nothing is disallowed. There is no admin area, no search-result page and no
# faceted navigation — the three things robots.txt normally exists to fence
# off. `site/preview/` is gitignored and never deployed, so the staging build
# is not on the live host to exclude. The file is here for the Sitemap line.
ROBOTS_BODY = f"""\
# {brand.SITE_NAME} — {brand.PUBLICATION}
# Every item on this site credits and links its source; nothing is reproduced.
# Questions, or a request to be delisted: {brand.CONTACT_EMAIL}

User-agent: *
Allow: /

Sitemap: {SITEMAP_URL}
"""


def write_robots(out_dir: Path) -> Path:
    """Write robots.txt. Static — it says the same thing every week."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ROBOTS
    path.write_text(ROBOTS_BODY, encoding="utf-8")
    return path


def urls(out_dir: Path) -> list[tuple[str, str | None]]:
    """Every public URL, with its last-modified date where one is known.

    Returned separately from the rendering so a test can assert the set of URLs
    without matching XML, and so it is obvious that this reads the archive
    index rather than scanning the output directory.
    """
    entries = archive.load(out_dir)
    dates = sorted({str(e["date"]) for e in entries if e.get("date")}, reverse=True)
    newest = dates[0] if dates else None

    found: list[tuple[str, str | None]] = [
        # The front page carries the current issue, so its lastmod is that
        # issue's date. It is also the page most likely to be crawled first.
        (f"{brand.SITE_URL}/", newest),
        (f"{brand.SITE_URL}/{archive.PAGE}", newest),
    ]
    found += [(f"{brand.SITE_URL}/{page}", None) for page in STATIC_PAGES]

    # The permalinks, newest first. These are the reason this file exists: they
    # are linked only from the archive page, so a crawler that never reaches
    # archive.html never learns a single back issue is there.
    for entry in entries:
        href = str(entry.get("href", ""))
        date = str(entry.get("date", ""))
        if not href or not date:
            log.warning("archive entry has no href or date, left out of the sitemap: %r", entry)
            continue
        found.append((f"{brand.SITE_URL}/{href}", date))
    return found


def sitemap_xml(out_dir: Path) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod in urls(out_dir):
        lines.append("  <url>")
        lines.append(f"    <loc>{escape(loc)}</loc>")
        if lastmod:
            lines.append(f"    <lastmod>{escape(lastmod)}</lastmod>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def write_sitemap(out_dir: Path) -> Path:
    """Write sitemap.xml from the archive index.

    Deliberately carries no `changefreq` or `priority`. Google has said in
    public that it ignores both, and a field a consumer ignores is a field that
    can drift from the truth without anyone noticing.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / SITEMAP
    path.write_text(sitemap_xml(out_dir), encoding="utf-8")
    return path


def write_all(out_dir: Path) -> tuple[Path, Path]:
    """Both files. Call after the archive index is updated, not before."""
    return write_robots(out_dir), write_sitemap(out_dir)
