"""Entity map tests.

These matter more than they look. The industry beat's whole value is getting
ownership right, and the brief is explicit that the common failure is inferring
ownership from a distribution or integration relationship. These tests pin the
map's shape so a careless edit can't quietly introduce that error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dailydive.entities import EntityKind, EntityMap, load_entities

MAP_PATH = Path("industry.toml")


@pytest.fixture(scope="module")
def emap() -> EntityMap:
    return load_entities(MAP_PATH)


def test_map_loads_and_parents_resolve(emap: EntityMap):
    assert len(emap.by_id) > 15
    for entity in emap.by_id.values():
        if entity.parent:
            assert entity.parent in emap.by_id


def test_ownership_chain_resolves_to_the_sponsor(emap: EntityMap):
    chain = [e.id for e in emap.chain("brs")]
    assert chain == ["brs", "aperture", "bertram"]


def test_product_brand_resolves_to_its_manufacturer(emap: EntityMap):
    """Radion is an EcoTech product line, not a company."""
    found = emap.find("EcoTech announces a new Radion generation")
    assert "ecotech" in [e.id for e in found]
    assert [e.id for e in emap.chain("ecotech")] == ["ecotech", "aperture", "bertram"]


def test_red_dragon_is_royal_exclusiv_not_a_separate_company(emap: EntityMap):
    found = emap.find("Red Dragon 3 speedy pump revision announced")
    assert [e.id for e in found] == ["royal-exclusiv"]


def test_hydro_wizard_belongs_to_panta_rhei_under_kag(emap: EntityMap):
    found = emap.find("Hydro Wizard ECM 63 used in a public aquarium build")
    assert [e.id for e in found] == ["panta-rhei"]
    assert [e.id for e in emap.chain("panta-rhei")] == ["panta-rhei", "kag"]


def test_jecod_and_jebao_are_the_same_company(emap: EntityMap):
    assert emap.find("Jecod DCP 5000 review")[0].id == "jebao"
    assert emap.find("Jebao announces a new factory")[0].id == "jebao"


def test_ambiguous_aliases_are_not_matched(emap: EntityMap):
    """'AI' is an AquaIllumination alias and also means something else entirely.

    Matching it automatically would tag every AI story as aquarium-lighting news.
    """
    assert emap.find("How AI is changing aquarium hobbyist software") == []
    assert emap.find("A new Apex release is coming") == []
    assert emap.find("Prime rate cut expected") == []


def test_full_names_still_match_when_the_short_form_is_ambiguous(emap: EntityMap):
    assert emap.find("AquaIllumination ships a new fixture")[0].id == "aqua-illumination"
    assert emap.find("Neptune Systems posts a firmware update")[0].id == "neptune"


def test_longest_alias_wins(emap: EntityMap):
    """'Bulk Reef Supply' must not be shadowed by a shorter overlapping alias."""
    found = emap.find("Bulk Reef Supply announces a store expansion")
    assert found[0].id == "brs"


def test_ownership_language_uses_portfolio_company_for_the_sponsor(emap: EntityMap):
    described = emap.describe_ownership("brs")
    assert "Bulk Reef Supply" in described
    assert "parent: Aperture Pet & Life" in described
    assert "portfolio company of Bertram Capital" in described
    assert "owns" not in described.lower()


def test_public_company_is_described_as_shareholder_owned(emap: EntityMap):
    described = emap.describe_ownership("iwaki")
    assert "shareholder-owned" in described
    assert "TSE:6237" in described


def test_distributors_are_not_recorded_as_owners(emap: EntityMap):
    """CoralVue distributes Abyzz; Aperture once distributed Maxspect.

    Neither is ownership, so neither may appear as a parent in the map.
    """
    assert emap.by_id["abyzz"].parent == "venotec"
    assert emap.by_id["maxspect"].parent is None
    assert "coralvue" not in emap.by_id


def test_pan_world_is_not_under_iwaki(emap: EntityMap):
    assert emap.by_id["pan-world"].parent is None


def test_entity_kinds_are_sane(emap: EntityMap):
    assert emap.by_id["bertram"].kind is EntityKind.SPONSOR
    assert emap.by_id["aperture"].kind is EntityKind.PARENT
    assert emap.by_id["helloreef"].kind is EntityKind.BRAND


def test_find_returns_multiple_entities_in_one_story(emap: EntityMap):
    found = {e.id for e in emap.find("SICCE and TUNZE both showed new pumps at the trade show")}
    assert found == {"sicce", "tunze"}
