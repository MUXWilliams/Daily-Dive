"""The back issues, and a page that lists them.

Dated permalinks have existed since the first build, and nothing linked to
them — an archive that only works if you already know the URL is a directory,
not an archive.

The index is a JSON sidecar rather than a scan of the HTML. Two reasons: the
issue pages are output, and parsing your own output to recover what you already
knew is the kind of loop that breaks quietly the first time a template changes;
and a machine-readable list of issues is the natural shape for the RSS feed that
comes next.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .models import Issue

log = logging.getLogger(__name__)

INDEX = "issues/index.json"
PAGE = "archive.html"

# Enough to build a useful line per issue without duplicating the issue itself.
# Deliberately not the whole item list: this file is an index, and an index that
# grows with the archive's contents rather than its length stops being one.
Entry = dict[str, object]


def load(out_dir: Path) -> list[Entry]:
    """Existing entries, newest first. Missing or unreadable means empty."""
    path = out_dir / INDEX
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt index must not take the issue down with it. Losing the
        # archive listing is recoverable; failing the run is not.
        log.error("could not read %s (%s) — starting a fresh index", path, exc)
        return []
    return data if isinstance(data, list) else []


def entry_for(issue: Issue) -> Entry:
    # The item a reader actually sees first, which is the first item of the
    # first section — not issue.items[0], which is the highest-scoring item.
    # Those stopped being the same thing when Community moved to the top of the
    # stack, and an archive line that names a story the reader has to scroll to
    # find is describing a different page than the one it links to.
    from . import render

    # The Resource video is excluded for the same reason: it renders at the foot
    # of the page, so naming it here would describe a story the reader has to
    # scroll past everything else to reach.
    sections = render.group_by_category(issue, exclude=render.pick_resource(issue))
    lead = sections[0][2][0].title if sections else ""
    return {
        "date": f"{issue.date:%Y-%m-%d}",
        "href": f"issues/{issue.date:%Y-%m-%d}.html",
        "items": len(issue.items),
        "outlets": len(issue.outlets),
        "lead": lead,
    }


def record(out_dir: Path, issue: Issue) -> list[Entry]:
    """Add this issue to the index, replacing any entry for the same date.

    Same-date replacement matters more than it looks: a re-run on the same day
    overwrites the dated permalink, so the index has to describe the page that
    is actually there rather than the first one that was.
    """
    stamp = f"{issue.date:%Y-%m-%d}"
    entries = [e for e in load(out_dir) if e.get("date") != stamp]
    entries.append(entry_for(issue))
    entries.sort(key=lambda e: str(e.get("date", "")), reverse=True)

    path = out_dir / INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")
    return entries


def write_page(out_dir: Path) -> Path | None:
    """Render the archive listing. Returns the path, or None if there's nothing."""
    from . import render

    entries = load(out_dir)
    if not entries:
        return None

    # The index stores an ISO string, which is the right thing on disk and the
    # wrong thing for a date filter that formats datetimes. Parsed here rather
    # than stored twice, so the file keeps one representation of a date.
    view = []
    for entry in entries:
        try:
            when = datetime.fromisoformat(str(entry["date"]))
        except (KeyError, ValueError):
            log.warning("archive entry has no readable date, skipped: %r", entry)
            continue
        view.append({**entry, "when": when})

    env = render._env()
    env.filters["datefmt"] = render._datefmt
    html = env.get_template("archive.html.j2").render(
        entries=view,
        brand=render.brand,
        header=render.find_header_image(out_dir),
    )
    path = out_dir / PAGE
    path.write_text(html, encoding="utf-8")
    return path
