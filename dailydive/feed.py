"""An RSS feed of the issues — the second door in, beside the email list.

Feed readers and aggregators follow a site without ranking and without an
address, which makes this the one distribution path that works while a brand-new
domain has no inbound links (see `docs/indexing.md`). Generated from
`site/issues/index.json`, the same sidecar `seo.py` builds the sitemap from, so
nothing here parses the site's own HTML and the feed can never list an issue the
archive does not.

One entry per issue, linking to its permalink. The description is the issue's
size and its lead story's headline, both already recorded in the index; nothing
is invented and no model writes it. Items are not reproduced, for the same
reason the pages carry a gist and not the story: this credits other people's
reporting, and the reader follows the link to it.

Publication times are the issue's date at 00:00 GMT. The index records a date,
not a moment, and RFC 822 insists on a time; a round one is honest about the
precision, where a plausible 07:00 would claim a minute nobody measured.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import archive, brand

log = logging.getLogger(__name__)

FEED = "feed.xml"
FEED_URL = f"{brand.SITE_URL}/{FEED}"

# A reader fetches the whole file each poll, and anyone who wants the rest has
# the archive page. Twenty weeks is most of half a year.
MAX_ENTRIES = 20


def _rfc822(date: str) -> str:
    moment = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return format_datetime(moment, usegmt=True)


def _description(entry: dict) -> str:
    lead = str(entry.get("lead", "")).strip()
    items, outlets = entry.get("items"), entry.get("outlets")
    size = f"{items} stories from {outlets} outlets" if items and outlets else ""
    return ". ".join(part for part in (f"Lead: {lead}" if lead else "", size) if part)


def feed_xml(out_dir: Path) -> str:
    entries = []
    for entry in archive.load(out_dir):
        href, date = str(entry.get("href", "")), str(entry.get("date", ""))
        try:
            published = _rfc822(date)
        except ValueError:
            log.warning("archive entry has no usable href or date, left out of the feed: %r", entry)
            continue
        if not href:
            log.warning("archive entry has no usable href or date, left out of the feed: %r", entry)
            continue
        entries.append((date, href, published, entry))
    entries = sorted(entries, key=lambda e: e[0], reverse=True)[:MAX_ENTRIES]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{escape(brand.PUBLICATION)} · {escape(brand.SITE_NAME)}</title>",
        f"    <link>{escape(brand.SITE_URL)}/</link>",
        f"    <description>{escape(brand.DESCRIPTION)}</description>",
        "    <language>en</language>",
        f'    <atom:link href="{escape(FEED_URL)}" rel="self" type="application/rss+xml"/>',
    ]
    if entries:
        lines.append(f"    <lastBuildDate>{entries[0][2]}</lastBuildDate>")
    for date, href, published, entry in entries:
        link = f"{brand.SITE_URL}/{href}"
        lines += [
            "    <item>",
            f"      <title>{escape(brand.PUBLICATION)} — {escape(date)}</title>",
            f"      <link>{escape(link)}</link>",
            f'      <guid isPermaLink="true">{escape(link)}</guid>',
            f"      <pubDate>{published}</pubDate>",
            f"      <description>{escape(_description(entry))}</description>",
            "    </item>",
        ]
    lines += ["  </channel>", "</rss>"]
    return "\n".join(lines) + "\n"


def write_feed(out_dir: Path) -> Path:
    """Write feed.xml. Call after the archive index is updated, not before."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / FEED
    path.write_text(feed_xml(out_dir), encoding="utf-8")
    return path
