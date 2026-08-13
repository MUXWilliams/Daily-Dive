"""Token accounting.

Cost visibility is the point of this module: without it, every efficiency
question about the pipeline is unanswerable. Prices are per million tokens,
current as of 2026-08. Verify against the pricing docs before trusting a
number you're going to act on — these are cached constants, not an API call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# model id -> (input $/MTok, output $/MTok)
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}

# Cache writes cost more than plain input; cache reads cost far less.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


@dataclass
class Spend:
    """Running token and cost totals for one stage of a run."""

    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    errors: int = 0

    def add(self, usage: object) -> None:
        """Accumulate one response's usage block.

        Reads defensively: the usage object gains fields over time, and a
        missing one should not take down a pipeline run.
        """
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    @property
    def cost_usd(self) -> float:
        in_price, out_price = PRICES.get(self.model, (0.0, 0.0))
        per_token_in = in_price / 1_000_000
        return (
            self.input_tokens * per_token_in
            + self.cache_write_tokens * per_token_in * CACHE_WRITE_MULTIPLIER
            + self.cache_read_tokens * per_token_in * CACHE_READ_MULTIPLIER
            + self.output_tokens * (out_price / 1_000_000)
        )

    @property
    def cache_hit_rate(self) -> float:
        """Share of prompt tokens served from cache.

        Zero across repeated runs means something is invalidating the prefix —
        the system prompt is meant to be byte-identical every call.
        """
        total = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return self.cache_read_tokens / total if total else 0.0

    def summary(self) -> str:
        return (
            f"{self.model}: {self.calls} calls, "
            f"{self.input_tokens + self.cache_read_tokens + self.cache_write_tokens:,} in / "
            f"{self.output_tokens:,} out, "
            f"cache hit {self.cache_hit_rate:.0%}, "
            f"${self.cost_usd:.4f}"
            + (f", {self.errors} errors" if self.errors else "")
        )


@dataclass
class RunSpend:
    """Per-stage spend for a whole run."""

    stages: dict[str, Spend] = field(default_factory=dict)

    def stage(self, name: str, model: str) -> Spend:
        return self.stages.setdefault(name, Spend(model=model))

    @property
    def total_usd(self) -> float:
        return sum(s.cost_usd for s in self.stages.values())

    def report(self) -> str:
        lines = [f"  {name}: {spend.summary()}" for name, spend in self.stages.items()]
        lines.append(f"  total: ${self.total_usd:.4f}")
        return "\n".join(lines)
