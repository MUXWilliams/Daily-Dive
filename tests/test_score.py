"""v1 scoring tests — no network, no API key, no cost.

The client is a stand-in that records what it was asked and replays canned
structured output. That covers the parts most likely to break silently in
production: prompt-cache invalidation, uid mismatches, and the filter rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from dailydive import score as score_mod
from dailydive.entities import IndustryBeat
from dailydive.models import Category, Item
from dailydive.pricing import Spend
from dailydive.score import ItemScore, ScoreBatch, apply_scores, score_items


def item(uid_seed: str, **kw) -> Item:
    defaults = dict(
        source_id="s",
        source_name="Reef Builders",
        title=f"Headline {uid_seed}",
        url=f"https://example.invalid/{uid_seed}",
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return Item(**{**defaults, **kw})


class FakeClient:
    """Records requests and replays a scripted ScoreBatch per call."""

    def __init__(self, batches: list[ScoreBatch] | Exception, usage=None):
        self._batches = batches
        self.requests: list[dict] = []
        self._usage = usage or SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=4096,
        )
        self.messages = self

    def parse(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self._batches, Exception):
            raise self._batches
        batch = self._batches[len(self.requests) - 1]
        return SimpleNamespace(parsed_output=batch, usage=self._usage)


def score(uid: str, **kw) -> ItemScore:
    defaults = dict(uid=uid, category=Category.INDUSTRY, relevance=0.8, is_promo=False, gist="g")
    return ItemScore(**{**defaults, **kw})


# ------------------------------------------------------------- prompt hygiene

def test_system_prompt_is_byte_identical_across_calls():
    """Anything interpolated per-request would invalidate the cache silently."""
    items = [item("a"), item("b")]
    client = FakeClient([ScoreBatch(scores=[score(i.uid) for i in items])])
    score_items(items, client=client)

    system = client.requests[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == score_mod.load_system_prompt()


def test_per_item_data_goes_in_the_user_turn_not_the_system_prompt():
    it = item("a", title="Fluval announces a gyre pump")
    client = FakeClient([ScoreBatch(scores=[score(it.uid)])])
    score_items([it], client=client)

    req = client.requests[0]
    assert "Fluval" not in req["system"][0]["text"]
    assert "Fluval" in req["messages"][0]["content"]


def test_source_category_hint_is_not_shown_to_the_model():
    """The hint says where an item came from, not what it is — offering it
    would just invite the model to rubber-stamp it."""
    it = item("a", category_hint=Category.VIDEO)
    client = FakeClient([ScoreBatch(scores=[score(it.uid)])])
    score_items([it], client=client)

    assert "Video" not in client.requests[0]["messages"][0]["content"]


def test_entity_context_is_attached_when_a_company_is_mentioned():
    it = item("a", title="EcoTech Marine ships a new Radion")
    client = FakeClient([ScoreBatch(scores=[score(it.uid)])])
    score_items([it], client=client)

    content = client.requests[0]["messages"][0]["content"]
    assert "Bertram Capital" in content  # resolved through the ownership chain


# -------------------------------------------------------------------- batching

def test_items_are_split_into_batches():
    items = [item(str(n)) for n in range(45)]
    batches = [
        ScoreBatch(scores=[score(i.uid) for i in items[0:20]]),
        ScoreBatch(scores=[score(i.uid) for i in items[20:40]]),
        ScoreBatch(scores=[score(i.uid) for i in items[40:45]]),
    ]
    client = FakeClient(batches)
    results = score_items(items, client=client, batch_size=20)

    assert len(client.requests) == 3
    assert len(results) == 45


def test_a_failed_batch_is_skipped_not_fatal():
    """A partial issue beats no issue."""
    items = [item("a"), item("b")]
    client = FakeClient(RuntimeError("api exploded"))
    spend = Spend(model=score_mod.MODEL)

    assert score_items(items, client=client, spend=spend) == {}
    assert spend.errors == 1


def test_empty_input_makes_no_api_call():
    client = FakeClient([])
    assert score_items([], client=client) == {}
    assert client.requests == []


# ------------------------------------------------------------ hallucinated uids

def test_unknown_uid_is_dropped():
    """A uid the model invented would attach a score to the wrong story."""
    it = item("a")
    client = FakeClient([ScoreBatch(scores=[score(it.uid), score("totally-made-up")])])
    results = score_items([it], client=client)

    assert set(results) == {it.uid}


# --------------------------------------------------------------------- filters

def test_promos_are_dropped_regardless_of_relevance():
    it = item("a")
    kept = apply_scores([it], {it.uid: score(it.uid, is_promo=True, relevance=0.95)})
    assert kept == []


def test_uncategorized_items_are_dropped():
    it = item("a")
    kept = apply_scores([it], {it.uid: score(it.uid, category=None, relevance=0.9)})
    assert kept == []


def test_low_relevance_is_dropped_at_the_threshold():
    a, b = item("a"), item("b")
    scores = {a.uid: score(a.uid, relevance=0.34), b.uid: score(b.uid, relevance=0.36)}
    kept = apply_scores([a, b], scores, threshold=0.35)
    assert [i.uid for i in kept] == [b.uid]


def test_unscored_items_are_dropped_by_default():
    """Publishing an unscored item means publishing something nothing judged."""
    it = item("a")
    assert apply_scores([it], {}) == []
    assert len(apply_scores([it], {}, keep_unscored=True)) == 1


def test_scores_replace_the_category_and_attach_the_gist():
    it = item("a", category_hint=Category.COMMUNITY)
    scored = score(it.uid, category=Category.HUSBANDRY, gist="dinos beaten with UV", relevance=0.7)
    kept = apply_scores([it], {it.uid: scored})

    assert kept[0].category_hint is Category.HUSBANDRY
    assert kept[0].extra["gist"] == "dinos beaten with UV"
    assert kept[0].extra["relevance"] == "0.70"


def test_industry_beat_is_carried_through():
    it = item("a")
    scored = score(it.uid, category=Category.INDUSTRY, beat=IndustryBeat.DISTRIBUTION)
    kept = apply_scores([it], {it.uid: scored})
    assert kept[0].extra["beat"] == "Distribution"


def test_kept_items_are_ordered_most_relevant_first():
    a, b, c = item("a"), item("b"), item("c")
    scores = {
        a.uid: score(a.uid, relevance=0.5),
        b.uid: score(b.uid, relevance=0.9),
        c.uid: score(c.uid, relevance=0.7),
    }
    kept = apply_scores([a, b, c], scores)
    assert [i.uid for i in kept] == [b.uid, c.uid, a.uid]


def test_attribution_survives_scoring():
    """model_copy must not disturb the fields the renderer requires."""
    it = item("a")
    kept = apply_scores([it], {it.uid: score(it.uid)})
    assert kept[0].source_name == it.source_name
    assert kept[0].url == it.url


# --------------------------------------------------------------------- pricing

def test_spend_accounting_matches_published_rates():
    spend = Spend(model="claude-haiku-4-5")
    spend.add(
        SimpleNamespace(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
    )
    assert spend.cost_usd == pytest.approx(1.00)  # $1/MTok input

    spend_out = Spend(model="claude-haiku-4-5")
    spend_out.add(
        SimpleNamespace(
            input_tokens=0,
            output_tokens=1_000_000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
    )
    assert spend_out.cost_usd == pytest.approx(5.00)  # $5/MTok output


def test_cache_reads_are_cheaper_than_plain_input():
    cached = Spend(model="claude-haiku-4-5")
    cached.add(
        SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=1_000_000,
        )
    )
    assert cached.cost_usd == pytest.approx(0.10)
    assert cached.cache_hit_rate == pytest.approx(1.0)


def test_usage_missing_fields_does_not_crash_accounting():
    spend = Spend(model="claude-haiku-4-5")
    spend.add(SimpleNamespace(input_tokens=10))  # older/partial usage shape
    assert spend.calls == 1
    assert spend.output_tokens == 0
