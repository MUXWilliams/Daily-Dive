"""Load sources.toml into Source models."""

from __future__ import annotations

import tomllib
from pathlib import Path

from .models import Source

DEFAULT_SOURCES = Path("sources.toml")


def load_sources(path: Path = DEFAULT_SOURCES, *, include_disabled: bool = False) -> list[Source]:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    sources = [Source(**entry) for entry in raw.get("source", [])]

    ids = [s.id for s in sources]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate source ids in {path}: {sorted(duplicates)}")

    return sources if include_disabled else [s for s in sources if s.enabled]
