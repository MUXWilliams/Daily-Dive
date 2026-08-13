"""Entity recognition for the industry beat.

Given a headline and summary text, work out which aquarium-equipment companies
are involved and what sits above them in the ownership chain. This runs with no
model calls: it is deterministic string matching over the map in industry.toml,
which makes it cheap, testable, and — more importantly — incapable of inventing
an ownership relationship that isn't in the file.

The editorial rules this supports live in docs/industry-brief.md. The one worth
repeating here: distribution, OEM manufacturing, and product integration are
NOT ownership, and nothing in this module should ever imply otherwise.
"""

from __future__ import annotations

import re
import tomllib
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

DEFAULT_INDUSTRY_MAP = Path("industry.toml")


class EntityKind(StrEnum):
    SPONSOR = "sponsor"
    PARENT = "parent"
    MANUFACTURER = "manufacturer"
    BRAND = "brand"


class IndustryBeat(StrEnum):
    """Sub-label for an industry item, per the brief's item format.

    Ordered by the brief's editorial priority — ownership news outranks a
    product launch.
    """

    OWNERSHIP = "Ownership"
    LEADERSHIP = "Leadership"
    DISTRIBUTION = "Distribution"
    PRODUCT = "Product"
    SAFETY = "Safety"
    MANUFACTURING = "Manufacturing"
    FINANCIAL = "Financial"


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    kind: EntityKind
    parent: str | None = None
    aliases: list[str] = []
    # Real aliases that are too generic to match automatically ("AI", "Prime",
    # "Apex"). Kept for the record; deliberately not matched on.
    ambiguous_aliases: list[str] = []
    public_ticker: str | None = None
    note: str | None = None


class EntityMap:
    def __init__(self, entities: list[Entity]) -> None:
        self.by_id = {e.id: e for e in entities}
        # Longest aliases first so "Bulk Reef Supply" wins over a shorter
        # substring match, and each alias maps to exactly one entity.
        pairs: list[tuple[str, str]] = []
        for entity in entities:
            for alias in entity.aliases:
                pairs.append((alias, entity.id))
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        self._patterns = [
            (re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE), eid) for alias, eid in pairs
        ]

    def chain(self, entity_id: str) -> list[Entity]:
        """The ownership chain upward, starting with the entity itself.

        Cycle-guarded: a malformed map should not hang the pipeline.
        """
        out: list[Entity] = []
        seen: set[str] = set()
        current = self.by_id.get(entity_id)
        while current and current.id not in seen:
            seen.add(current.id)
            out.append(current)
            current = self.by_id.get(current.parent) if current.parent else None
        return out

    def find(self, *texts: str | None) -> list[Entity]:
        """Entities mentioned in the given text, most specific first.

        Returns only entities matched directly — parents are reachable via
        chain(), but are not asserted as "mentioned" just because a child was.
        """
        haystack = " ".join(t for t in texts if t)
        if not haystack:
            return []

        found: list[str] = []
        for pattern, entity_id in self._patterns:
            if entity_id in found:
                continue
            if pattern.search(haystack):
                found.append(entity_id)
        return [self.by_id[i] for i in found]

    def describe_ownership(self, entity_id: str) -> str:
        """A precise one-line ownership statement, safe to render.

        Uses the brief's vocabulary: "portfolio company" for a PE sponsor,
        "parent company" otherwise. Never says "owns" about a relationship the
        map doesn't record as ownership.
        """
        chain = self.chain(entity_id)
        if len(chain) < 2:
            entity = self.by_id.get(entity_id)
            if entity and entity.public_ticker:
                return f"{entity.name} — shareholder-owned, {entity.public_ticker}"
            return chain[0].name if chain else entity_id

        parts = [chain[0].name]
        for parent in chain[1:]:
            label = "portfolio company of" if parent.kind is EntityKind.SPONSOR else "parent:"
            parts.append(f"{label} {parent.name}")
        return " — ".join(parts)


@lru_cache(maxsize=4)
def load_entities(path: Path = DEFAULT_INDUSTRY_MAP) -> EntityMap:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    entities = [Entity(**e) for e in raw.get("entity", [])]

    ids = {e.id for e in entities}
    for entity in entities:
        if entity.parent and entity.parent not in ids:
            raise ValueError(f"{entity.id}: unknown parent {entity.parent!r}")
    return EntityMap(entities)
