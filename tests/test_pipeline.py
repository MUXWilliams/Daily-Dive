"""v0 pipeline tests. No network, no model calls, no cost."""

from __future__ import annotations

import re

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from dailydive import cli, config, normalize, render, store
from dailydive.models import (
    AttributionError,
    Category,
    Issue,
    Item,
    Source,
    SourceType,
    canonicalize_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_source(sid: str, **kw) -> Source:
    defaults = dict(id=sid, name=sid, url=f"https://example.invalid/{sid}.rss")
    return Source(**{**defaults, **kw})


def item(**kw) -> Item:
    defaults = dict(
        source_id="s",
        source_name="Some Outlet",
        title="A headline",
        url="https://example.invalid/a",
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return Item(**{**defaults, **kw})


# --------------------------------------------------------------- attribution

def test_item_requires_a_resolvable_url():
    with pytest.raises((AttributionError, ValidationError)):
        item(url="/relative/path")
    with pytest.raises((AttributionError, ValidationError)):
        item(url="javascript:alert(1)")


def test_item_requires_a_source_name():
    with pytest.raises((AttributionError, ValidationError)):
        item(source_name="   ")


def test_item_requires_a_title():
    with pytest.raises((AttributionError, ValidationError)):
        item(title="")


def test_render_refuses_an_uncreditable_item():
    """The invariant must hold at the publish boundary, not just at parse time."""
    good = item()
    smuggled = good.model_copy(update={"source_name": ""})  # bypasses validators
    issue = Issue(date=datetime(2026, 8, 12, tzinfo=UTC), items=[smuggled])

    with pytest.raises(AttributionError):
        render.render_issue(issue)


def test_every_rendered_item_links_to_its_source():
    issue = Issue(date=datetime(2026, 8, 12, tzinfo=UTC), items=[item(), item(url="https://other.invalid/b")])
    html = render.render_issue(issue)

    for it in issue.items:
        assert it.url in html
        assert it.source_name in html


def test_footer_credits_every_outlet():
    issue = Issue(
        date=datetime(2026, 8, 12, tzinfo=UTC),
        items=[item(source_name="Outlet A"), item(url="https://b.invalid/x", source_name="Outlet B")],
    )
    assert issue.outlets == ["Outlet A", "Outlet B"]
    html = render.render_issue(issue)
    assert "Outlet A" in html and "Outlet B" in html


# ------------------------------------------------------------------- dedupe

def test_canonicalize_strips_tracking_and_trailing_slash():
    a = canonicalize_url("https://WWW.Example.com/post/?utm_source=rss&id=7#comments")
    b = canonicalize_url("https://www.example.com/post?id=7")
    assert a == b


def test_dedupe_collapses_the_same_thread_arriving_twice():
    source = fixture_source("reef2reef", type=SourceType.XENFORO)
    items = normalize.normalize(source, (FIXTURES / "reef2reef.xml").read_bytes())
    assert len(items) == 3  # fixture deliberately contains a duplicate

    unique = normalize.dedupe(items)
    assert len(unique) == 2
    assert len({i.uid for i in unique}) == 2


# ---------------------------------------------------------------- normalize

def test_wordpress_feed_yields_credited_items():
    source = fixture_source("reefbuilders", name="Reef Builders", type=SourceType.WORDPRESS)
    items = normalize.normalize(source, (FIXTURES / "reefbuilders.xml").read_bytes())

    assert len(items) == 3
    first = items[0]
    assert first.source_name == "Reef Builders"
    assert first.author == "Jane Reefer"
    assert first.published_at.year == 2026
    assert first.raw_text and "<p>" not in first.raw_text  # markup stripped


def test_youtube_feed_captures_video_id_without_transcripts():
    source = fixture_source("yt-brs", name="BRStv", type=SourceType.YOUTUBE, category_hint=Category.VIDEO)
    items = normalize.normalize(source, (FIXTURES / "yt-brs.xml").read_bytes())

    assert len(items) == 2
    assert items[0].extra.get("video_id")
    assert items[0].category_hint is Category.VIDEO


def test_section_makes_the_credit_more_specific():
    source = fixture_source("reef2reef", name="Reef2Reef", section="Reef Chemistry")
    assert source.display_name == "Reef2Reef — Reef Chemistry"


def test_entries_without_a_date_are_dropped_not_guessed():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><title>Undated</title><link>https://example.invalid/undated</link></item>
    </channel></rss>"""
    assert normalize.normalize(fixture_source("x"), feed) == []


# -------------------------------------------------------------------- config

def test_sources_toml_parses_and_ids_are_unique():
    sources = config.load_sources(Path("sources.toml"), include_disabled=True)
    assert sources
    assert len({s.id for s in sources}) == len(sources)


def test_disabled_sources_are_excluded_by_default():
    all_sources = config.load_sources(Path("sources.toml"), include_disabled=True)
    enabled = config.load_sources(Path("sources.toml"))
    assert len(enabled) < len(all_sources)
    assert all(s.enabled for s in enabled)


# --------------------------------------------------------------------- store

def test_archive_dedupes_across_runs(tmp_path):
    db = tmp_path / "t.sqlite3"
    items = [item(), item(url="https://example.invalid/b")]

    with store.connect(db) as conn:
        assert store.record_items(conn, items) == 2
    with store.connect(db) as conn:
        assert store.record_items(conn, items) == 0  # same morning, re-run
        assert store.known_uids(conn, [i.uid for i in items]) == {i.uid for i in items}


def test_conditional_get_headers_round_trip(tmp_path):
    db = tmp_path / "t.sqlite3"
    url = "https://example.invalid/feed"
    with store.connect(db) as conn:
        assert store.get_cache_headers(conn, url) == {}
        store.save_cache_headers(conn, url, '"abc123"', "Wed, 12 Aug 2026 14:00:00 GMT")
    with store.connect(db) as conn:
        headers = store.get_cache_headers(conn, url)
    assert headers["If-None-Match"] == '"abc123"'
    assert headers["If-Modified-Since"].startswith("Wed, 12 Aug 2026")


# -------------------------------------------------------------------- render

def test_sections_follow_canonical_order_and_skip_empties():
    issue = Issue(
        date=datetime(2026, 8, 12, tzinfo=UTC),
        items=[
            item(url="https://a.invalid/1", category_hint=Category.COMMUNITY),
            item(url="https://a.invalid/2", category_hint=Category.INDUSTRY),
            item(url="https://a.invalid/3"),  # no hint -> "Elsewhere"
        ],
    )
    buckets = render.group_by_category(issue)
    assert [t for t, _, _ in buckets] == [
        Category.INDUSTRY.value, Category.COMMUNITY.value, "Elsewhere"
    ]
    # Slug keys the section's colour, so it must survive alongside the title.
    assert [sl for _, sl, _ in buckets] == ["industry", "community", "elsewhere"]


def test_empty_issue_still_renders():
    html = render.render_issue(Issue(date=datetime(2026, 8, 12, tzinfo=UTC), items=[]))
    assert "Nothing new in the feeds" in html


def test_verbose_flag_is_accepted_on_either_side_of_the_subcommand(tmp_path):
    """argparse exits 2 on an unrecognized flag; CI passes -v after the
    subcommand, so both positions must parse."""
    from dailydive import cli

    for argv in (["-v", "sources"], ["sources", "-v"]):
        assert cli.main(argv) == 0


def test_unknown_source_id_is_an_error_not_an_empty_issue(tmp_path):
    from dailydive import cli

    assert cli.main(["run", "--source", "nope", "--offline", "--out", str(tmp_path)]) == 2


def test_dates_render_without_platform_specific_codes():
    """`%-d` is glibc-only and raises on Windows. Regression guard."""
    dt = datetime(2026, 8, 3, 9, 31, tzinfo=UTC)
    assert render._datefmt(dt) == "August 3, 2026"
    assert render._datefmt(dt, "full") == "Monday, August 3, 2026"
    assert render._datefmt(dt, "short") == "Aug 3"

    template = (render.TEMPLATE_DIR / "issue.html.j2").read_text(encoding="utf-8")
    assert "%-" not in template

    html = render.render_issue(Issue(date=dt, items=[item()]))
    assert "August 3, 2026" in html


# --------------------------------------------------------------------- probe

def test_autodiscovery_skips_wordpress_boilerplate_feeds():
    """Comment streams and oEmbed endpoints are advertised but never news."""
    from dailydive import probe as probe_mod

    for junk in (
        "https://reefs.com/comments/feed/",
        "https://reefs.com/wp-json/oembed/1.0/embed?url=x&format=xml",
        "https://ecotechmarine.com/web-stories/feed/",
    ):
        assert any(m in junk.lower() for m in probe_mod._JUNK_FEED_MARKERS)

    assert not any(m in "https://reefbuilders.com/feed/" for m in probe_mod._JUNK_FEED_MARKERS)


def test_feed_link_parser_finds_advertised_feeds():
    from dailydive.probe import _FeedLinkParser

    parser = _FeedLinkParser()
    parser.feed(
        '<html><head>'
        '<link rel="alternate" type="application/rss+xml" title="Feed" href="/feed/">'
        '<link rel="alternate" type="application/json+oembed" href="/oembed">'
        '<link rel="stylesheet" href="/style.css">'
        '</head></html>'
    )
    hrefs = [h for h, _ in parser.feeds]
    assert "/feed/" in hrefs
    assert "/style.css" not in hrefs


def test_every_category_has_a_slug_and_a_colour_in_the_stylesheet():
    """A category with no matching CSS class would render on the fallback hue —
    silently, and only in production."""
    template = (render.TEMPLATE_DIR / "issue.html.j2").read_text(encoding="utf-8")
    for category in Category:
        assert f".sec-{category.slug}" in template, category
    assert ".sec-elsewhere" in template  # the uncategorized fallback


def test_slugs_are_css_safe():
    for category in Category:
        assert category.slug.replace("-", "").isalnum(), category
        assert category.slug.islower()


def test_page_commits_to_one_theme():
    """Light-only is a decision, not an omission — no dark-mode blocks, and the
    body paints its own background so it never borrows the host's."""
    template = (render.TEMPLATE_DIR / "issue.html.j2").read_text(encoding="utf-8")
    assert "prefers-color-scheme" not in template
    assert "data-theme" not in template
    assert "background: var(--page)" in template


# -------------------------------------------------------------------- banner

def test_banner_is_used_when_present_and_carries_the_heading(tmp_path):
    """The artwork contains the wordmark, so the h1 wraps the image and the alt
    text is the heading — no duplicated text, and a real <h1> for a reader."""
    (tmp_path / "assets").mkdir()
    (tmp_path / render.HEADER_IMAGE).write_bytes(b"\x89PNG\r\n\x1a\n")

    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=[item()])
    html = render.render_issue(issue, header_image=render.find_header_image(tmp_path))

    assert render.HEADER_IMAGE in html
    # The class name also appears in the stylesheet, so check the markup.
    assert 'class="banner-fallback"' not in html
    assert '<h1 class="wordmark">' in html


def test_missing_banner_falls_back_rather_than_breaking(tmp_path):
    """The artwork is an upgrade, never a dependency."""
    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=[item()])
    assert render.find_header_image(tmp_path) is None

    html = render.render_issue(issue, header_image=None)
    assert 'class="banner-fallback"' in html
    assert "Daily Dive" in html  # the wordmark still reaches the page


def test_banner_path_resolves_from_the_dated_permalink(tmp_path):
    """index.html and issues/*.html sit at different depths; an absolute path
    would work live and break when opened off disk, so both get a relative one."""
    (tmp_path / "assets").mkdir()
    (tmp_path / render.HEADER_IMAGE).write_bytes(b"\x89PNG\r\n\x1a\n")

    assert render.find_header_image(tmp_path) == render.HEADER_IMAGE
    assert render.find_header_image(tmp_path, depth=1) == "../" + render.HEADER_IMAGE


def test_write_issue_gives_each_page_the_right_banner_path(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / render.HEADER_IMAGE).write_bytes(b"\x89PNG\r\n\x1a\n")

    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=[item()])
    render.write_issue(issue, tmp_path)

    index = (tmp_path / "index.html").read_text()
    dated = (tmp_path / "issues" / "2026-08-13.html").read_text()
    assert f'src="{render.HEADER_IMAGE}"' in index
    assert f'src="../{render.HEADER_IMAGE}"' in dated


# --------------------------------------------------------------------- intro

def test_intro_highlights_come_from_the_issue_itself():
    """Derived from the ranked items, never written separately — so the intro
    can't advertise a story the issue doesn't contain."""
    items = [item(url=f"https://a.invalid/{n}", title=f"Story {n}") for n in range(6)]
    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=items)

    bullets, plus = render.highlights(issue, limit=4)
    assert bullets == [f"Story {n}" for n in range(4)]
    assert plus and plus.startswith("Plus 2 more")

    html = render.render_issue(issue)
    for line in bullets:
        assert line in html


def test_intro_omits_the_plus_line_when_nothing_is_left_over():
    items = [item(url=f"https://a.invalid/{n}", title=f"Story {n}") for n in range(3)]
    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=items)

    bullets, plus = render.highlights(issue, limit=4)
    assert len(bullets) == 3
    assert plus is None


def test_intro_plus_line_names_the_remaining_areas():
    items = [
        item(url="https://a.invalid/1", category_hint=Category.INDUSTRY),
        item(url="https://a.invalid/2", category_hint=Category.INDUSTRY),
        item(url="https://a.invalid/3", category_hint=Category.VIDEO),
        item(url="https://a.invalid/4", category_hint=Category.WILD_REEFS),
    ]
    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=items)
    _, plus = render.highlights(issue, limit=2)
    assert "video" in plus and "wild reefs" in plus


def test_empty_issue_shows_no_intro():
    """Greeting a reader and then showing them nothing is worse than silence."""
    html = render.render_issue(Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=[]))
    assert "Howdy" not in html
    assert "Nothing new in the feeds" in html


def test_greeting_uses_the_issue_date_not_today():
    """A dated permalink read next week must still say the day it was written."""
    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=[item()])  # a Thursday
    assert "Happy Thursday" in render.render_issue(issue)


# ------------------------------------------------------------------- contact

def test_one_contact_address_everywhere():
    """A publisher asking to be delisted must find the same address wherever
    they look — the User-Agent, the about page, or the README."""
    from dailydive import brand, ingest

    assert brand.CONTACT_EMAIL in ingest.USER_AGENT
    assert brand.SITE_URL in ingest.USER_AGENT
    assert brand.CONTACT_EMAIL in Path("site/about.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------- youtube

def test_youtube_sources_carry_a_real_channel_id():
    """A wrong or placeholder channel id returns an empty feed rather than an
    error — the worst failure shape, because it looks like a quiet channel."""
    for source in config.load_sources(Path("sources.toml"), include_disabled=True):
        if source.type is not SourceType.YOUTUBE:
            continue
        assert "channel_id=" in source.url, source.id
        cid = source.url.split("channel_id=")[1]
        assert cid.startswith("UC"), f"{source.id}: {cid!r} is not a channel id"
        assert len(cid) == 24, f"{source.id}: channel id should be 24 chars, got {len(cid)}"
        assert "REPLACE" not in cid.upper(), f"{source.id} still has a placeholder"


def test_no_source_ships_with_a_placeholder_url():
    for source in config.load_sources(Path("sources.toml"), include_disabled=True):
        assert "REPLACE" not in source.url.upper(), source.id
        assert "example.com" not in source.url, source.id


# ------------------------------------------------------------------ workflow

WORKFLOW = Path(".github/workflows/daily.yml")


def _run_flags_in_workflow() -> set[str]:
    """Long flags the workflow hands to `daily-dive run`."""
    text = WORKFLOW.read_text(encoding="utf-8")
    return set(re.findall(r'ARGS="\$ARGS (--[a-z-]+)', text))


def test_workflow_only_passes_flags_the_cli_accepts():
    """CI builds its argv with shell string-splicing, so a renamed flag isn't a
    syntax error anywhere — it's an exit code 2 after install and tests have
    already gone green. Catch it here instead."""
    parser = cli.build_parser()
    run_parser = parser._subparsers._group_actions[0].choices["run"]  # noqa: SLF001
    known = {opt for action in run_parser._actions for opt in action.option_strings}  # noqa: SLF001

    flags = _run_flags_in_workflow()
    assert flags, "found no flags in the workflow — did the build step change shape?"
    assert flags <= known, f"workflow passes unknown flags: {sorted(flags - known)}"


def test_workflow_scoring_is_opt_in_and_has_a_key():
    """Scoring is the only step that spends money, so it must never be the
    default, and it must fail loudly rather than half-run without a key."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--score" in text
    assert "ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}" in text
    score_input = text.split("      score:", 1)[1].split("      threshold:", 1)[0]
    assert "default: false" in score_input


def test_verbose_is_accepted_on_either_side_of_the_subcommand():
    """CI passes -v after the subcommand and humans type it before. Both have
    to actually turn on debug logging — the failure mode here is silent, not a
    parse error."""
    parser = cli.build_parser()
    assert getattr(parser.parse_args(["-v", "run"]), "verbose", False)
    assert getattr(parser.parse_args(["run", "-v"]), "verbose", False)
    assert not getattr(parser.parse_args(["run"]), "verbose", False)
