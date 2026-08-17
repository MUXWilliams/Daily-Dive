"""Measuring the scorer against the editor.

The project has 219 tests and every one of them checks that the pipeline
*works* — feeds parse, items carry credit, sections order correctly. None of
them check whether the scoring is any *good*. That is the difference between a
correctness test and a quality measurement, and until the second one exists,
every question about the scorer ("is the bridge rule holding", "is 0.45 the
right line", "would a bigger model help") can only be argued about.

This module closes that. The editor labels a set of real items by hand, the
current prompt scores the same items, and the two are compared.

Two design decisions worth keeping:

**Only items that actually reached the scorer are eligible.** The seen log holds
808 items; roughly 128 of them were ever scored. The rest were dropped by the
recency filter before any model saw them — YouTube hands back fifty videos per
channel on first fetch, most of them years old. Labelling those would measure
nothing, at the cost of an hour.

**The labelling sheet never shows the model's score.** A number in view anchors
the judgement immediately, and an anchored label is an expensive way to confirm
what the model already thought. `build_sheet` is written so the score is not
available to render even by accident, and a test asserts it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import Category, Item

log = logging.getLogger(__name__)

# What the editor picks from. Ordinal rather than a 0-1 number: people are far
# more consistent choosing between four labelled options than at picking a
# decimal, whose meaning drifts over a session — a 0.6 at item 10 does not mean
# the same as a 0.6 at item 90.
#
# The bands exist to map a label onto the threshold decision. They are not a
# claim that the editor is thinking in decimals.
BUCKETS: dict[str, tuple[float, float]] = {
    "lead": (0.85, 1.00),
    "include": (0.60, 0.85),
    "borderline": (0.45, 0.60),
    "drop": (0.00, 0.45),
}

# Which labels mean "this belongs in the issue". Borderline is deliberately in
# neither set — see `report`.
KEEP = frozenset({"lead", "include"})
DROP = frozenset({"drop"})


def eligible(conn: sqlite3.Connection, *, max_age_days: int = 7) -> list[Item]:
    """Items that plausibly reached the scorer, newest first.

    The test is `first_seen_at - published_at <= max_age_days`: an item the
    crawler saw while it was still inside the recency window. This is a proxy,
    not a record — nothing logs which items were passed to the model. It is
    right for the ordinary case and wrong for a feed that backfills, where an
    old item genuinely arrives for the first time today.

    The proxy exists because it is cheap and because the alternative was
    sampling from the whole seen log, which is 84% items no model ever judged.
    Once the `scores` table has real history, prefer that: it is a record rather
    than an inference.
    """
    rows = conn.execute(
        "SELECT * FROM items"
        " WHERE julianday(first_seen_at) - julianday(published_at) <= ?"
        " ORDER BY published_at DESC",
        (max_age_days,),
    )
    return [_item(row) for row in rows]


def _item(row: sqlite3.Row) -> Item:
    hint = row["category_hint"]
    return Item(
        source_id=row["source_id"],
        source_name=row["source_name"],
        title=row["title"],
        url=row["url"],
        published_at=datetime.fromisoformat(row["published_at"]),
        author=row["author"],
        raw_text=row["raw_text"],
        category_hint=Category(hint) if hint else None,
    )


# --------------------------------------------------------------- the sheet


def build_sheet(items: list[Item], *, title: str = "Label these") -> str:
    """Render the labelling page.

    Note what is passed to the template: uid, outlet, title, date, an excerpt
    and the URL. No relevance, no category the model assigned, no gist it wrote
    — not because the template is trusted to ignore them, but because they are
    not there to be shown. Anchoring is not a discipline problem; it is what
    reading a number does to you.
    """
    from . import render

    payload = [
        {
            "uid": i.uid,
            "source": i.source_name,
            "title": i.title,
            "url": i.url,
            "date": f"{i.published_at:%Y-%m-%d}",
            "excerpt": _excerpt(i.raw_text),
        }
        for i in items
    ]
    env = render._env()
    return env.get_template("label-sheet.html.j2").render(
        items=payload,
        items_json=json.dumps(payload),
        buckets=list(BUCKETS),
        title=title,
    )


def _excerpt(text: str | None, words: int = 60) -> str:
    """Enough of the body to judge the subject, not enough to read instead."""
    if not text:
        return ""
    parts = text.split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def load_labels(path: Path, *, known: set[str] | None = None) -> dict[str, str]:
    """Parse exported labels into uid -> bucket.

    Validates the bucket names, and — when given the set we asked about — that
    every uid is one of them. A label file that has drifted from the item set
    produces a number that looks fine and means nothing, which is worse than an
    error.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "labels" in raw:
        raw = raw["labels"]

    labels: dict[str, str] = {}
    for uid, bucket in dict(raw).items():
        bucket = str(bucket).strip().lower()
        if bucket not in BUCKETS:
            raise ValueError(f"{uid}: unknown bucket {bucket!r}; expected one of {list(BUCKETS)}")
        if known is not None and uid not in known:
            raise ValueError(f"{uid}: labelled but not in the item set")
        labels[uid] = bucket
    return labels


# --------------------------------------------------------------- the report


@dataclass
class Disagreement:
    uid: str
    title: str
    source_id: str
    label: str
    relevance: float


@dataclass
class Report:
    """The comparison. `false_negatives` is the deliverable; the rest is context."""

    threshold: float
    judged: int = 0
    agreed: int = 0
    borderline: list[Disagreement] = field(default_factory=list)
    false_negatives: list[Disagreement] = field(default_factory=list)
    false_positives: list[Disagreement] = field(default_factory=list)
    unscored: list[str] = field(default_factory=list)
    category_judged: int = 0
    category_agreed: int = 0

    @property
    def accuracy(self) -> float:
        return self.agreed / self.judged if self.judged else 0.0

    def by_source(self) -> dict[str, int]:
        """Disagreements per source. A source that consistently confuses the
        scorer is a config problem — wrong category_hint, wrong feed — not a
        prompt problem, and the two get fixed in different files."""
        counts: dict[str, int] = {}
        for d in self.false_negatives + self.false_positives:
            counts[d.source_id] = counts.get(d.source_id, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def report(
    labels: dict[str, str],
    scores: dict[str, object],
    items: dict[str, Item],
    *,
    threshold: float,
) -> Report:
    """Compare labels against scores at the shipping threshold.

    Borderline-labelled items are counted and listed but excluded from the
    headline figure. Being marked wrong for disagreeing about something the
    editor themselves called a coin flip is noise, and noise in the headline
    number is how a measurement stops being believed.
    """
    rep = Report(threshold=threshold)

    for uid, bucket in labels.items():
        item = items.get(uid)
        if item is None:
            continue
        score = scores.get(uid)
        if score is None:
            # Scored nothing for this item — a failed batch, or a re-run that
            # did not cover it. Counted, never silently treated as a drop.
            rep.unscored.append(uid)
            continue

        relevance = float(score.relevance)
        row = Disagreement(uid, item.title, item.source_id, bucket, relevance)
        model_keeps = relevance >= threshold

        if bucket == "borderline":
            rep.borderline.append(row)
        else:
            rep.judged += 1
            editor_keeps = bucket in KEEP
            if editor_keeps == model_keeps:
                rep.agreed += 1
            elif editor_keeps:
                rep.false_negatives.append(row)
            else:
                rep.false_positives.append(row)

        # Category is judged only where both sides think the item belongs —
        # asking where a dropped item "should" have been filed is a question
        # neither party answered.
        assigned = getattr(score, "category", None)
        if model_keeps and bucket in KEEP and assigned is not None and item.category_hint:
            rep.category_judged += 1
            if assigned == item.category_hint:
                rep.category_agreed += 1

    rep.false_negatives.sort(key=lambda d: d.relevance)
    rep.false_positives.sort(key=lambda d: -d.relevance)
    return rep


def format_report(rep: Report) -> str:
    """The report as text, for a terminal or a CI log."""
    lines = [
        f"Judged {rep.judged} items at threshold {rep.threshold:.2f}"
        f" — agreed on {rep.agreed} ({rep.accuracy:.0%})",
        f"Borderline, excluded from that figure: {len(rep.borderline)}",
    ]
    if rep.unscored:
        lines.append(f"Labelled but never scored: {len(rep.unscored)}")
    if rep.category_judged:
        share = rep.category_agreed / rep.category_judged
        lines.append(
            f"Category agreement where both sides kept the item:"
            f" {rep.category_agreed}/{rep.category_judged} ({share:.0%})"
        )

    lines += [
        "",
        f"## False negatives — you'd have run these, the model dropped them ({len(rep.false_negatives)})",
        "   These never reach a page, so nothing in production would show them.",
    ]
    for d in rep.false_negatives:
        lines.append(f"  [{d.relevance:.2f}] ({d.label}) {d.title}  — {d.source_id}")

    lines += ["", f"## False positives — the model ran these, you'd have dropped them ({len(rep.false_positives)})"]
    for d in rep.false_positives:
        lines.append(f"  [{d.relevance:.2f}] {d.title}  — {d.source_id}")

    counts = rep.by_source()
    if counts:
        lines += ["", "## Disagreements by source"]
        lines += [f"  {sid}: {n}" for sid, n in counts.items()]
    return "\n".join(lines)
