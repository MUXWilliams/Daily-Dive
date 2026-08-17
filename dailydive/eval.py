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

# Ordinal rank for the correlation. The gaps are not meaningful — only the
# order is — which is exactly what a rank correlation assumes.
ORDINAL = {"drop": 0, "borderline": 1, "include": 2, "lead": 3}

# How many items an issue actually carries. `precision_at(ISSUE_SIZE)` is the
# number that describes what a reader receives, as opposed to what the scorer
# believes.
ISSUE_SIZE = 20


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
#
# What the labels can and cannot support.
#
# The labels answer "does this belong in an issue at all" — admissibility. They
# were made without counting slots. The threshold answers a different question:
# an issue has room for about twenty items and the threshold is what keeps it to
# twenty. That is rationing, not admission.
#
# Conflating the two produces a false-negative list of everything the editor
# would have allowed and the threshold rationed away, which is technically
# correct and practically useless — and a useless number is how a measurement
# stops being believed. So the report is built around the comparisons the labels
# genuinely license:
#
#   * an editor DROP that scored above threshold is an error, full stop;
#   * an editor LEAD that scored below threshold is an error, full stop — a
#     lead is not a marginal call, it is a story the editor would have put at
#     the top of a section;
#   * an editor INCLUDE below threshold may be an error or may be correct
#     rationing. Listed, never counted.
#
# And the production question is ranking rather than classification: of the
# twenty items that would actually ship, how many did the editor want?


@dataclass
class Row:
    uid: str
    title: str
    source_id: str
    label: str
    relevance: float


@dataclass
class Report:
    """`leads_missed` is the deliverable. The rest is context for it."""

    threshold: float
    scored: int = 0

    # Unambiguous, in both directions.
    leads_missed: list[Row] = field(default_factory=list)
    drops_admitted: list[Row] = field(default_factory=list)

    # Real disagreements that the labels cannot adjudicate.
    includes_below: list[Row] = field(default_factory=list)
    borderline: list[Row] = field(default_factory=list)

    ranked: list[Row] = field(default_factory=list)   # every scored row, best first
    unscored: list[str] = field(default_factory=list)
    rho: float | None = None

    category_judged: int = 0
    category_agreed: int = 0

    def precision_at(self, n: int) -> tuple[int, int]:
        """Of the model's top n, how many the editor would have admitted.

        Returns (hits, considered). `considered` is min(n, scored) rather than n,
        so a run with fewer than n scored items reports what it actually
        measured instead of quietly counting missing items as misses.
        """
        top = self.ranked[:n]
        return sum(1 for r in top if r.label in KEEP), len(top)

    def by_source(self) -> dict[str, int]:
        """Unambiguous errors per source. A source that consistently confuses
        the scorer is a config problem — wrong feed, wrong category_hint — and
        gets fixed in sources.toml, not in the prompt."""
        counts: dict[str, int] = {}
        for r in self.leads_missed + self.drops_admitted:
            counts[r.source_id] = counts.get(r.source_id, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def _ranks(values: list[float]) -> list[float]:
    """Tie-corrected ranks: tied values share their average rank.

    Ties are the normal case here, not an edge case — the editor's scale has
    four values across 128 items, so almost everything is tied. Ranking them
    arbitrarily would invent an ordering the editor never expressed.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rho: Pearson on tie-corrected ranks.

    Written out rather than imported. scipy is a large dependency to add for one
    coefficient in a project whose whole architecture is "no server, no
    dependencies that need patching".

    Returns None when either series is constant — the correlation is undefined
    there, and returning 0.0 would read as "no relationship" when the truth is
    "not measurable".
    """
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy) ** 0.5


def report(
    labels: dict[str, str],
    scores: dict[str, object],
    items: dict[str, Item],
    *,
    threshold: float,
) -> Report:
    """Compare labels against scores. See the note above for what is countable."""
    rep = Report(threshold=threshold)
    xs: list[float] = []
    ys: list[float] = []

    for uid, bucket in labels.items():
        item = items.get(uid)
        if item is None:
            continue
        score = scores.get(uid)
        if score is None:
            # A failed batch, or a re-run that did not cover it. Counted, never
            # silently treated as a drop — that would invent agreement.
            rep.unscored.append(uid)
            continue

        relevance = float(score.relevance)
        row = Row(uid, item.title, item.source_id, bucket, relevance)
        rep.scored += 1
        rep.ranked.append(row)
        xs.append(relevance)
        ys.append(float(ORDINAL[bucket]))

        above = relevance >= threshold
        if bucket == "borderline":
            rep.borderline.append(row)
        elif bucket == "lead" and not above:
            rep.leads_missed.append(row)
        elif bucket == "drop" and above:
            rep.drops_admitted.append(row)
        elif bucket == "include" and not above:
            rep.includes_below.append(row)

        assigned = getattr(score, "category", None)
        if above and bucket in KEEP and assigned is not None and item.category_hint:
            rep.category_judged += 1
            if assigned == item.category_hint:
                rep.category_agreed += 1

    rep.ranked.sort(key=lambda r: -r.relevance)
    rep.leads_missed.sort(key=lambda r: r.relevance)
    rep.drops_admitted.sort(key=lambda r: -r.relevance)
    rep.includes_below.sort(key=lambda r: r.relevance)
    rep.rho = spearman(xs, ys)
    return rep


def format_report(rep: Report) -> str:
    """The report as markdown, for a terminal, a CI log, or docs/eval/."""
    if not rep.scored:
        return (
            f"No scored items among {len(rep.unscored)} labelled.\n"
            "Nothing to measure — run with --rescore, or check that the prompt "
            "hash matches the one the scores were stored under."
        )

    hits20, of20 = rep.precision_at(ISSUE_SIZE)
    hits10, of10 = rep.precision_at(10)
    lines = [
        f"Scored {rep.scored} labelled items at threshold {rep.threshold:.2f}.",
        "",
        "## What a reader would receive",
        f"- Top {of20} by relevance: **{hits20}/{of20} ({hits20/of20:.0%})** the editor would admit",
        f"- Top {of10}: **{hits10}/{of10} ({hits10/of10:.0%})**",
    ]
    if rep.rho is not None:
        lines.append(f"- Rank agreement (Spearman ρ): **{rep.rho:+.2f}**")
    if rep.category_judged:
        share = rep.category_agreed / rep.category_judged
        lines.append(
            f"- Category agreement where both sides kept the item:"
            f" {rep.category_agreed}/{rep.category_judged} ({share:.0%})"
        )
    if rep.unscored:
        lines.append(f"- Labelled but never scored: {len(rep.unscored)}")

    lines += [
        "",
        f"## Leads the model buried ({len(rep.leads_missed)})",
        "Editor marked these a lead; the model scored them below threshold, so",
        "they never reached a page. Nothing in production would reveal them.",
        "",
    ]
    lines += [f"- `{r.relevance:.2f}` {r.title} — *{r.source_id}*" for r in rep.leads_missed] or ["_None._"]

    lines += [
        "",
        f"## Items the model ran that the editor would drop ({len(rep.drops_admitted)})",
        "",
    ]
    lines += [f"- `{r.relevance:.2f}` {r.title} — *{r.source_id}*" for r in rep.drops_admitted] or ["_None._"]

    lines += [
        "",
        f"## Admitted by the editor, below threshold ({len(rep.includes_below)})",
        "Not counted as errors. The editor judged whether these *belong*; the",
        "threshold decides how many *fit*. Some of this list is correct rationing.",
        "",
    ]
    lines += [f"- `{r.relevance:.2f}` {r.title} — *{r.source_id}*" for r in rep.includes_below] or ["_None._"]

    counts = rep.by_source()
    if counts:
        lines += ["", "## Unambiguous errors by source", ""]
        lines += [f"- `{sid}`: {n}" for sid, n in counts.items()]
    return "\n".join(lines)
