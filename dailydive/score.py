"""v1 scoring pass — Haiku assigns a category, relevance, and gist per item.

This is the cheap tier of the two-tier design. It runs over every item so the
expensive writing pass (v2) only ever sees the survivors.

Three things worth knowing before editing:

1. **Structured output, not prose.** `messages.parse()` validates the response
   against a pydantic schema, so a malformed reply is an exception here rather
   than a mystery three stages downstream.

2. **The system prompt is a frozen prefix.** It is loaded verbatim from
   prompts/score.system.md and marked cacheable. Interpolating anything
   per-request — a date, a run id — would invalidate the cache on every call
   and silently multiply cost. Per-item data goes in the user turn.

3. **Entity context is supplied, not inferred.** Detected companies come from
   the deterministic matcher in entities.py, so the model reasons about
   ownership it was handed rather than ownership it remembers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from .entities import IndustryBeat, load_entities
from .models import Category, Item
from .pricing import Spend

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "score.system.md"

# Items per request. Large enough to amortize the cached system prompt across
# many items; small enough that one malformed reply doesn't cost the whole run.
BATCH_SIZE = 20

# Below this, an item never reaches the issue. Raised from 0.35 after the first
# run with the marine-science accounts: 26 of 46 published items were Wild
# Reefs, and the 0.4 band was full of items whose own gist called them
# "tangential" or "minimal aquarium application". The prompt now forbids that
# hedge-and-publish combination, and this is the second line of defence.
DEFAULT_THRESHOLD = 0.45

# The attribution policy caps summaries at 40 words. The prompt asks for two
# sentences within that budget, but a prompt is a request and this is the rule:
# an over-long gist is dropped rather than published. The item still runs with
# its own headline, so enforcement costs a summary, never a story.
GIST_MAX_WORDS = 40


class ItemScore(BaseModel):
    """One scored item. `category=None` means drop it."""

    uid: str
    category: Category | None = None
    relevance: float = Field(ge=0.0, le=1.0)
    is_promo: bool = False
    # Required, with no default. `= ""` satisfies the schema without the model
    # writing anything, and the first live run showed exactly that: the five
    # highest-scoring industry stories came back with no summary, because an
    # omitted field was legal. Requiring it makes the schema do the asking.
    gist: str
    beat: IndustryBeat | None = None


class ScoreBatch(BaseModel):
    scores: list[ItemScore]


def load_system_prompt(path: Path = PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


def _item_payload(item: Item) -> dict[str, object]:
    """What the model sees for one item.

    Deliberately excludes the source's own category_hint: the hint reflects
    where the item came from, not what it is, and offering it invites the
    model to rubber-stamp it. Entity context is included because the model
    cannot derive ownership on its own and must not guess at it.
    """
    payload: dict[str, object] = {
        "uid": item.uid,
        "source": item.source_name,
        "title": item.title,
        "text": (item.raw_text or "")[:600],
        "published": item.published_at.date().isoformat(),
    }
    try:
        entities = load_entities().find(item.title, item.raw_text)
    except (OSError, ValueError) as exc:  # missing or malformed industry.toml
        log.warning("entity map unavailable (%s); scoring without entity context", exc)
        entities = []
    if entities:
        emap = load_entities()
        payload["entities"] = [emap.describe_ownership(e.id) for e in entities]
    return payload


def _chunk(items: list[Item], size: int) -> list[list[Item]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def score_items(
    items: list[Item],
    *,
    client: object,
    spend: Spend | None = None,
    batch_size: int = BATCH_SIZE,
) -> dict[str, ItemScore]:
    """Score every item. Returns uid -> ItemScore.

    A batch that fails is logged and skipped rather than aborting the run: a
    partial issue beats no issue, and the unscored items simply don't appear.
    """
    if not items:
        return {}

    system = [
        {
            "type": "text",
            "text": load_system_prompt(),
            # Frozen prefix. Haiku's cache minimum is 4096 tokens, so short
            # prompts silently won't cache — check spend.cache_hit_rate rather
            # than assuming this is doing something.
            "cache_control": {"type": "ephemeral"},
        }
    ]

    results: dict[str, ItemScore] = {}
    for batch in _chunk(items, batch_size):
        payload = [_item_payload(i) for i in batch]
        try:
            response = client.messages.parse(
                model=MODEL,
                max_tokens=8000,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Score these {len(payload)} items.\n\n"
                            + json.dumps(payload, indent=1)
                        ),
                    }
                ],
                output_format=ScoreBatch,
            )
        except Exception as exc:  # noqa: BLE001 — one bad batch must not end the run
            log.error("scoring batch of %d failed: %s", len(batch), exc)
            if spend:
                spend.errors += 1
            continue

        if spend and getattr(response, "usage", None):
            spend.add(response.usage)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            log.error("scoring batch returned no parsed output")
            if spend:
                spend.errors += 1
            continue

        known = {i.uid for i in batch}
        for score in parsed.scores:
            # The model is told to echo uids verbatim; a mismatch means it
            # invented one, and an invented uid would attach a score to the
            # wrong story. Drop it rather than guess which item was meant.
            if score.uid not in known:
                log.warning("scorer returned unknown uid %r, dropped", score.uid)
                continue
            results[score.uid] = score

        missing = known - {s.uid for s in parsed.scores}
        if missing:
            log.warning("scorer omitted %d of %d items in a batch", len(missing), len(batch))

    return results


def apply_scores(
    items: list[Item],
    scores: dict[str, ItemScore],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    keep_unscored: bool = False,
) -> list[Item]:
    """Filter and re-categorize items from their scores.

    An item with no score is dropped by default — an unscored item is one the
    pipeline knows nothing about, and publishing it would mean publishing
    something nothing has judged. `keep_unscored` exists for the case where
    scoring failed wholesale and a degraded issue beats an empty one.
    """
    kept: list[Item] = []
    for item in items:
        score = scores.get(item.uid)
        if score is None:
            if keep_unscored:
                kept.append(item)
            continue
        if score.category is None or score.is_promo or score.relevance < threshold:
            continue

        gist = score.gist.strip()
        if len(gist.split()) > GIST_MAX_WORDS:
            log.warning(
                "gist for %s ran to %d words (cap %d), dropped",
                item.uid,
                len(gist.split()),
                GIST_MAX_WORDS,
            )
            gist = ""

        kept.append(
            item.model_copy(
                update={
                    "category_hint": score.category,
                    "extra": {
                        **item.extra,
                        **({"gist": gist} if gist else {}),
                        "relevance": f"{score.relevance:.2f}",
                        **({"beat": score.beat.value} if score.beat else {}),
                    },
                }
            )
        )

    # Most relevant first within the issue; the renderer sections by category.
    kept.sort(key=lambda i: float(i.extra.get("relevance", 0)), reverse=True)
    return kept
