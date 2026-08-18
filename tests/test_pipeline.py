"""v0 pipeline tests. No network, no model calls, no cost."""

from __future__ import annotations

import argparse
import email.message
import json
import re
import struct

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from dailydive import brand, cli, config, ingest, normalize, render, store
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


def _png(width: int, height: int) -> bytes:
    """Just enough PNG for the size reader: signature + IHDR length/type/dims."""
    import struct as _s
    return b"\x89PNG\r\n\x1a\n" + _s.pack(">I", 13) + b"IHDR" + _s.pack(">II", width, height)


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
    source = fixture_source("yt-brs", name="BRStv", type=SourceType.YOUTUBE)
    items = normalize.normalize(source, (FIXTURES / "yt-brs.xml").read_bytes())

    assert len(items) == 2
    assert items[0].extra.get("video_id")


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
        Category.COMMUNITY.value, Category.INDUSTRY.value, "Elsewhere"
    ]
    # Slug keys the section's colour, so it must survive alongside the title.
    assert [sl for _, sl, _ in buckets] == ["community", "industry", "elsewhere"]


def test_community_leads_the_issue():
    """An editorial call, not an accident of enum order: the reader is a
    hobbyist, and what other hobbyists are building is why they opened this."""
    assert list(Category)[0] is Category.COMMUNITY


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
    # A padding-stripped strftime code, not any "%-": Jinja's whitespace
    # control writes {%- and that is not a date format.
    assert not re.search(r"%-[a-zA-Z]", template)

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
    (tmp_path / "assets/masthead.png").write_bytes(_png(954, 202))

    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=[item()])
    html = render.render_issue(issue, header=render.find_header_image(tmp_path))

    assert "assets/masthead.png" in html
    # The class name also appears in the stylesheet, so check the markup.
    assert 'class="banner-fallback"' not in html
    assert '<h1 class="wordmark">' in html


def test_missing_banner_falls_back_rather_than_breaking(tmp_path):
    """The artwork is an upgrade, never a dependency."""
    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=[item()])
    assert render.find_header_image(tmp_path) is None

    html = render.render_issue(issue, header=None)
    assert 'class="banner-fallback"' in html
    assert brand.PUBLICATION in html  # the wordmark still reaches the page


def test_banner_path_resolves_from_the_dated_permalink(tmp_path):
    """index.html and issues/*.html sit at different depths; an absolute path
    would work live and break when opened off disk, so both get a relative one."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/masthead.png").write_bytes(_png(954, 202))

    assert render.find_header_image(tmp_path)[0] == "assets/masthead.png"
    assert render.find_header_image(tmp_path, depth=1)[0] == "../assets/masthead.png"


def test_write_issue_gives_each_page_the_right_banner_path(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/masthead.png").write_bytes(_png(954, 202))

    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=[item()])
    render.write_issue(issue, tmp_path)

    index = (tmp_path / "index.html").read_text()
    dated = (tmp_path / "issues" / "2026-08-13.html").read_text()
    assert 'src="assets/masthead.png"' in index
    assert 'src="../assets/masthead.png"' in dated


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
        item(url="https://a.invalid/3", category_hint=Category.COMMUNITY),
        item(url="https://a.invalid/4", category_hint=Category.WILD_REEFS),
    ]
    issue = Issue(date=datetime(2026, 8, 13, tzinfo=UTC), items=items)
    _, plus = render.highlights(issue, limit=2)
    assert "community" in plus and "wild reefs" in plus


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

def test_youtube_sources_carry_a_real_uploads_playlist_id():
    """A wrong id returns an empty result rather than an error — the worst
    failure shape, because a dead channel looks exactly like a quiet one.

    The uploads playlist id is the channel id with UC swapped for UU. Getting
    that swap wrong is the easy mistake, so it is the thing asserted."""
    for source in config.load_sources(Path("sources.toml"), include_disabled=True):
        if source.type is not SourceType.YOUTUBE_API:
            continue
        assert "playlistId=" in source.url, source.id
        pid = source.url.split("playlistId=")[1].split("&")[0]
        assert pid.startswith("UU"), f"{source.id}: {pid!r} is not an uploads playlist id"
        assert len(pid) == 24, f"{source.id}: playlist id should be 24 chars, got {len(pid)}"
        assert "REPLACE" not in pid.upper(), f"{source.id} still has a placeholder"


def test_youtube_api_sources_never_carry_a_key_in_the_config():
    """The key is a credential. It belongs in the environment, and a config
    file is committed to a repo."""
    raw = Path("sources.toml").read_text(encoding="utf-8")
    assert "key=" not in raw
    assert "AIza" not in raw  # Google API keys all start this way


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


def test_text_rendering_carries_credit_and_link_for_every_item():
    """The text form is a real output, not a debug print — the attribution rule
    applies to it exactly as it does to the page."""
    issue = Issue(date=datetime(2026, 8, 14, tzinfo=UTC), items=[item(extra={"gist": "A short gist.", "relevance": "0.80"})])
    text = render.as_text(issue)
    for published in issue.items:
        assert published.title in text
        assert published.source_name in text
        assert published.url in text


# ------------------------------------------------------------------ recency

def test_stale_items_are_dropped_before_they_cost_anything():
    """A feed carries whatever was posted last, not what was posted today. The
    first live run filed a March press release as news; this is the guard."""
    now = datetime(2026, 8, 14, tzinfo=UTC)
    fresh = item(url="https://reefbuilders.com/fresh/", published_at=now - timedelta(days=3))
    stale = item(url="https://reefbuilders.com/stale/", published_at=now - timedelta(days=90))
    kept = normalize.recent([fresh, stale], days=14, now=now)
    assert kept == [fresh]


def test_max_age_zero_keeps_everything():
    now = datetime(2026, 8, 14, tzinfo=UTC)
    old = item(published_at=now - timedelta(days=900))
    assert normalize.recent([old], days=0, now=now) == [old]


def test_scoring_requires_a_gist():
    """An optional gist is one the model can silently skip — and on the first
    live run it skipped exactly the highest-scoring stories."""
    from dailydive.score import ItemScore

    with pytest.raises(ValidationError):
        ItemScore(uid="x", relevance=0.9)


def test_volume_report_counts_each_window_per_outlet():
    """Whether this is a daily or a weekly is a question about how much the
    sources publish — which is measurable, so measure it."""
    now = datetime(2026, 8, 14, tzinfo=UTC)
    items = [
        item(url="https://reefbuilders.com/a/", published_at=now - timedelta(days=1)),
        item(url="https://reefbuilders.com/b/", published_at=now - timedelta(days=10)),
        item(url="https://reefbuilders.com/c/", published_at=now - timedelta(days=25)),
        item(url="https://reefbuilders.com/d/", published_at=now - timedelta(days=200)),
    ]
    report = normalize.volume_report(items, now=now)
    assert "ALL" in report
    assert report.strip().splitlines()[-1].split()[-3:] == ["1", "2", "3"]


# --------------------------------------------------------------------- probe

def _feed_with_date(when: datetime) -> bytes:
    return f"""<?xml version="1.0"?><rss version="2.0"><channel>
      <title>Test</title>
      <item><title>A post</title><link>https://example.org/a</link>
      <pubDate>{when:%a, %d %b %Y %H:%M:%S} +0000</pubDate></item>
    </channel></rss>""".encode()


def test_probe_flags_a_feed_that_parses_but_stopped_publishing():
    """Four configured sources probed 'ok' and were publishing nothing — one
    hadn't posted in three and a half years. Parsing is not liveness."""
    import feedparser

    from dailydive import probe as probe_mod

    fresh = feedparser.parse(_feed_with_date(datetime.now(UTC) - timedelta(days=2)))
    dead = feedparser.parse(_feed_with_date(datetime.now(UTC) - timedelta(days=1295)))

    assert probe_mod._newest_age_days(fresh) <= 3
    assert probe_mod._newest_age_days(dead) >= 1290


# --------------------------------------------------------------- youtube api

_YT_PAYLOAD = json.dumps(
    {
        "items": [
            {
                "snippet": {
                    "title": "We tested 6 salt mixes for 90 days",
                    "description": "Full results and methodology.",
                    "publishedAt": "2026-08-12T14:00:00Z",
                    "videoOwnerChannelTitle": "BRStv",
                    "resourceId": {"kind": "youtube#video", "videoId": "abc123XYZ_1"},
                }
            },
            {
                "snippet": {
                    "title": "Private video",
                    "description": "",
                    "publishedAt": "2026-08-11T14:00:00Z",
                    "resourceId": {"kind": "youtube#video", "videoId": "hidden00000"},
                }
            },
        ]
    }
).encode()


def _yt_source() -> Source:
    return Source(
        id="yt-brs",
        name="BRStv",
        url="https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId=UUx",
        type=SourceType.YOUTUBE_API,
        section="Bulk Reef Supply",
    )


def test_youtube_api_items_are_creditable_and_linkable():
    items = normalize.normalize(_yt_source(), _YT_PAYLOAD)
    assert len(items) == 1  # the private video is dropped
    video = items[0]
    assert video.url == "https://www.youtube.com/watch?v=abc123XYZ_1"
    assert video.source_name == "BRStv — Bulk Reef Supply"
    assert video.published_at == datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
    assert video.extra["video_id"] == "abc123XYZ_1"


def test_private_videos_are_dropped_rather_than_linked():
    """They stay in the uploads playlist as placeholders. Publishing one sends
    a reader to a video they cannot watch."""
    titles = [i.title for i in normalize.normalize(_yt_source(), _YT_PAYLOAD)]
    assert "Private video" not in titles


def test_youtube_api_error_response_yields_no_items_and_no_crash():
    body = json.dumps({"error": {"code": 403, "message": "quota exceeded"}}).encode()
    assert normalize.normalize(_yt_source(), body) == []


def test_api_key_comes_from_the_environment_and_is_not_the_cache_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaTESTKEY")
    fetcher = ingest.Fetcher(client=httpx.Client())
    url = fetcher._authorize(_yt_source())
    assert url.endswith("&key=AIzaTESTKEY")
    assert "key=" not in _yt_source().url
    fetcher.close()


def test_a_missing_api_key_is_a_clear_error_not_an_unauthenticated_request(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    fetcher = ingest.Fetcher(client=httpx.Client())
    with pytest.raises(ingest.MissingCredential):
        fetcher._authorize(_yt_source())
    fetcher.close()


def test_only_the_api_source_type_can_bypass_robots():
    """The robots exemption is a property of the type, so no ordinary feed can
    opt out of the check by editing sources.toml."""
    assert _yt_source().is_authorized_api
    for source in config.load_sources(Path("sources.toml"), include_disabled=True):
        if source.type is not SourceType.YOUTUBE_API:
            assert not source.is_authorized_api, source.id


# -------------------------------------------------------------------- shorts

def test_iso_durations_parse():
    from dailydive.youtube import parse_duration

    assert parse_duration("PT45S") == 45
    assert parse_duration("PT3M") == 180
    assert parse_duration("PT18M22S") == 1102
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("garbage") is None


def _durations_client(mapping: dict[str, str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["id"].split(",")
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": vid, "contentDetails": {"duration": mapping[vid]}}
                    for vid in ids
                    if vid in mapping
                ]
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _video(vid: str) -> Item:
    return item(url=f"https://www.youtube.com/watch?v={vid}", extra={"video_id": vid})


def test_shorts_are_dropped_and_real_videos_keep_their_duration():
    from dailydive import youtube

    items = [_video("short01"), _video("long001")]
    with _durations_client({"short01": "PT48S", "long001": "PT18M22S"}) as client:
        kept = youtube.drop_shorts(items, client=client, api_key="k")

    assert [i.extra["video_id"] for i in kept] == ["long001"]
    assert kept[0].extra["duration_s"] == "1102"


def test_a_video_exactly_at_the_cutoff_is_a_short():
    from dailydive import youtube

    with _durations_client({"edge0001": "PT3M"}) as client:
        assert youtube.drop_shorts([_video("edge0001")], client=client, api_key="k") == []


def test_a_video_whose_duration_cannot_be_resolved_is_kept():
    """Asymmetric failure: dropping a real video loses reporting silently,
    while keeping one Short is a blemish someone can point at."""
    from dailydive import youtube

    items = [_video("known001"), _video("missing1")]
    with _durations_client({"known001": "PT12M"}) as client:
        kept = youtube.drop_shorts(items, client=client, api_key="k")

    assert {i.extra["video_id"] for i in kept} == {"known001", "missing1"}


def test_non_video_items_pass_through_untouched():
    from dailydive import youtube

    article = item(url="https://reefbuilders.com/story/")
    with _durations_client({}) as client:
        assert youtube.drop_shorts([article], client=client, api_key="k") == [article]


# ------------------------------------------------------------ no video bucket

def test_there_is_no_video_category():
    """Video is a medium, not a subject. Having both meant the same story
    landed in different sections depending on how it was filmed."""
    assert "VIDEO" not in Category.__members__
    assert not any(c.value == "Video" for c in Category)


def test_the_scoring_prompt_offers_no_video_category():
    """The prompt is the model's only list of sections. If Video survives
    there, the model will keep filing videos by medium whatever the enum says."""
    prompt = Path("prompts/score.system.md").read_text(encoding="utf-8")
    assert "- **Video**" not in prompt
    for category in Category:
        assert f"**{category.value}**" in prompt, f"{category.value} missing from the prompt"


def test_runtime_reads_as_a_length_a_reader_can_judge():
    assert render._runtime(1102) == "18 min"
    assert render._runtime("240") == "4 min"
    assert render._runtime(4920) == "1h 22m"
    assert render._runtime(None) == ""


def test_duration_appears_on_the_page_but_never_as_a_section():
    # Two videos, because the best one is promoted to Resource — and the point
    # of this test is the one that stays behind: it is filed under what it is
    # about, not under its medium.
    promoted = item(
        title="How to quarantine a new fish",
        url="https://www.youtube.com/watch?v=abc123XYZ_0",
        category_hint=Category.HUSBANDRY,
        extra={"video_id": "abc123XYZ_0", "duration_s": "600", "relevance": "0.9"},
    )
    video = item(
        url="https://www.youtube.com/watch?v=abc123XYZ_1",
        category_hint=Category.HUSBANDRY,
        extra={"video_id": "abc123XYZ_1", "duration_s": "1102", "relevance": "0.5"},
    )
    issue = Issue(date=datetime(2026, 8, 14, tzinfo=UTC), items=[promoted, video])
    html = render.render_issue(issue)
    assert "18 min" in html
    assert "Husbandry &amp; Science" in html  # escaped, see autoescape test
    assert 'class="sec-video"' not in html


def test_feed_content_is_escaped_before_it_reaches_the_page():
    """Titles are attacker-controlled in the ordinary case — anyone who can
    post to a syndicated forum or blog can put markup in one. This failed
    silently for weeks because select_autoescape matches the final extension
    and the template is issue.html.j2, so ["html"] never matched."""
    nasty = item(
        title='Pump "review" <script>alert(1)</script> & more',
        url="https://reefbuilders.com/x/",
        category_hint=Category.INDUSTRY,
        extra={"gist": "Tag <b>soup</b> & ampersands."},
    )
    html = render.render_issue(Issue(date=datetime(2026, 8, 14, tzinfo=UTC), items=[nasty]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; more" in html
    assert "<b>soup</b>" not in html


# ------------------------------------------------------------------- bluesky

_BSKY_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>@icrs.bsky.social - International Coral Reef Society</title>
  <item>
    <description>Registration is open for the 2027 International Coral Reef
      Symposium in Auckland. Early-bird rates close in March.</description>
    <link>https://bsky.app/profile/icrs.bsky.social/post/aaa</link>
    <pubDate>Wed, 12 Aug 2026 09:00:00 +0000</pubDate>
  </item>
  <item>
    <description></description>
    <link>https://bsky.app/profile/icrs.bsky.social/post/bbb</link>
    <pubDate>Tue, 11 Aug 2026 09:00:00 +0000</pubDate>
  </item>
</channel></rss>"""


def test_bluesky_posts_get_a_headline_made_from_their_first_sentence():
    source = fixture_source(
        "icrs-bsky",
        name="International Coral Reef Society",
        type=SourceType.BLUESKY,
        section="Bluesky",
    )
    items = normalize.normalize(source, _BSKY_FEED)

    assert len(items) == 1  # the empty, image-only post is dropped
    post = items[0]
    assert post.title == (
        "Registration is open for the 2027 International Coral Reef Symposium in Auckland"
    )
    # The credit says where it came from: a post, not an article.
    assert post.source_name == "International Coral Reef Society — Bluesky"


def test_synth_title_prefers_a_whole_sentence_and_falls_back_to_a_clean_clip():
    from dailydive.normalize import _synth_title

    assert _synth_title("Short and done. Second sentence here.") == "Short and done"
    assert _synth_title(None) is None
    assert _synth_title("   ") is None

    long_one = _synth_title("word " * 60)
    assert long_one.endswith("…")
    assert len(long_one) <= normalize.SYNTH_TITLE_CHARS + 1
    assert not long_one.endswith(" …")


def test_a_textless_post_is_dropped_rather_than_published_as_a_bare_link():
    """An item with no title can't be credited or read — an image-only post
    has nothing to say in a digest."""
    from dailydive.normalize import _synth_title

    assert _synth_title("<img src='x'>") is None


def test_synth_title_does_not_cut_a_headline_at_an_abbreviation():
    """The first live run produced "The administration of U.S", "Our colleague
    Dr" and "#NewStudy by Menkara et al" — every one a truncation bug."""
    from dailydive.normalize import _synth_title

    assert _synth_title(
        "The administration of U.S. President Trump redirected funding. More."
    ) == "The administration of U.S. President Trump redirected funding"
    assert _synth_title("Our colleague Dr. Smith published a study. Next.") == (
        "Our colleague Dr. Smith published a study"
    )
    assert _synth_title("#NewStudy by Menkara et al. reclassifies genera. Next.") == (
        "#NewStudy by Menkara et al. reclassifies genera"
    )
    # An initial is not a sentence ending either.
    assert _synth_title("Named after Amanda V. Vincent this year. Next.") == (
        "Named after Amanda V. Vincent this year"
    )
    # A question mark still ends a headline, and keeps its mark.
    assert _synth_title("Is the reef recovering? Some think so.") == "Is the reef recovering?"


# ------------------------------------------------------------ near-duplicates

def test_the_same_story_from_several_accounts_collapses_to_one():
    """One new seahorse species arrived six times in a single issue, from two
    outlets. Items are scored before this runs, so the survivor is the
    best-scored telling."""
    titles = [
        "The newly described species, Hippocampus amandavincentae, has been named in honour of",
        "The newly discovered Indian Ocean species, Hippocampus Amandavincentae, was named after",
        "✨ Meet 𝘏𝘪𝘱𝘱𝘰𝘤𝘢𝘮𝘱𝘶𝘴 𝘢𝘮𝘢𝘯𝘥𝘢𝘷𝘪𝘯𝘤𝘦𝘯𝘵𝘢𝘦, named after Amanda Vincent, our director",
    ]
    items = [item(title=t, url=f"https://bsky.app/p/{n}") for n, t in enumerate(titles)]
    kept = normalize.collapse_similar(items)

    assert len(kept) == 1
    assert kept[0].title == titles[0]  # the first, i.e. the highest-scoring
    assert kept[0].extra["similar"] == "2"


def test_styled_unicode_is_folded_before_comparing():
    """A post written in mathematical italics shares no characters with the
    same words written plainly — without NFKC they never match."""
    plain = normalize._fingerprint("Hippocampus amandavincentae named")
    styled = normalize._fingerprint("𝘏𝘪𝘱𝘱𝘰𝘤𝘢𝘮𝘱𝘶𝘴 𝘢𝘮𝘢𝘯𝘥𝘢𝘷𝘪𝘯𝘤𝘦𝘯𝘵𝘢𝘦 𝘯𝘢𝘮𝘦𝘥")
    assert plain == styled


def test_unrelated_stories_are_never_merged():
    items = [
        item(title="El Nino is happening in an unnaturally warming world", url="https://a.invalid/1"),
        item(title="How to Treat Brown Jelly Disease", url="https://a.invalid/2"),
        item(title="Climate change is amplifying lionfish invasions globally", url="https://a.invalid/3"),
    ]
    assert len(normalize.collapse_similar(items)) == 3


def test_a_headline_too_short_to_judge_is_kept():
    """Two-word posts share words by accident. Keeping is the safe error."""
    items = [
        item(title="Big news", url="https://a.invalid/1"),
        item(title="Big news", url="https://a.invalid/2"),
    ]
    assert len(normalize.collapse_similar(items)) == 2


def test_the_prompt_forbids_hedging_and_publishing():
    """The scorer wrote "tangential to reef keeping" and then scored 0.4. The
    prompt has to make its own hedge binding, or the pattern comes back."""
    # Collapsed and de-emphasised: the rule wraps across lines and carries
    # markdown bold, and a test that breaks on re-wrapping is one nobody keeps.
    raw = Path("prompts/score.system.md").read_text(encoding="utf-8")
    prompt = " ".join(raw.replace("*", "").split())
    assert "tangential" in prompt
    assert "0.2 or below" in prompt
    # The hedge rule must stay about subject, not about difficulty — otherwise
    # it goes back to killing the deep science the issue exists to carry.
    assert "This is about subject, not about difficulty" in prompt


def test_the_prompt_gives_one_ordered_answer_per_beat():
    """AquaBiomics shutting down was tagged Ownership, then Distribution, then
    Financial across three runs. An ordered list is what makes it stable."""
    # Collapsed, because the rule is wrapped across lines in the prompt and a
    # test that breaks on re-wrapping is a test nobody will keep.
    prompt = " ".join(Path("prompts/score.system.md").read_text(encoding="utf-8").split())
    assert "shutting down, closing, or ceasing operations" in prompt
    assert "A shutdown is Financial, not Ownership" in prompt
    for beat in ("Ownership", "Financial", "Leadership", "Distribution", "Manufacturing", "Safety", "Product"):
        assert beat in prompt


# ------------------------------------------------------------------- weekly

WORKFLOW_TEXT = WORKFLOW.read_text(encoding="utf-8")


def test_the_schedule_is_weekly_and_matches_the_recency_window():
    """A window longer than the cadence republishes last issue's leftovers; a
    shorter one drops stories nobody has seen yet."""
    assert 'cron: "0 10 * * 5"' in WORKFLOW_TEXT  # Friday
    assert normalize.DEFAULT_MAX_AGE_DAYS == 7


def test_a_scheduled_run_still_scores():
    """inputs.* are empty on a schedule event, so a naive `inputs.score` test
    would leave the weekly issue unscored — raw feed dumps, silently."""
    assert "inputs.score || github.event_name == 'schedule'" in WORKFLOW_TEXT
    # And the key check must cover the scheduled path too, or the run dies at
    # the first model call instead of the first step.
    guard = WORKFLOW_TEXT.split("Check the API key is present", 1)[1][:200]
    assert "github.event_name == 'schedule'" in guard


# --------------------------------------------------------------- newsletters

def _newsletter(sender: str = "news@petage-email.com", html: str = "") -> "email.message.Message":
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = f"Pet Age <{sender}>"
    msg["Subject"] = "Pet Age Weekly"
    msg["Date"] = "Fri, 14 Aug 2026 09:00:00 +0000"
    msg.set_content("plain text fallback")
    msg.add_alternative(html or _NEWSLETTER_HTML, subtype="html")
    return msg


_NEWSLETTER_HTML = """
<html><body>
  <a href="https://petage.com/coral-aquaculture-expands">
     Marine ornamental aquaculture expands as coral farms scale up production</a>
  <a href="https://petage.com/premium-dog-food-trends">
     Premium dog food sales climb for the eighth consecutive quarter</a>
  <a href="https://petage.com/x">Read more</a>
  <a href="https://petage.com/unsub">Unsubscribe from this newsletter today</a>
</body></html>
"""


def _mail_source(senders=("news@petage-email.com",)) -> Source:
    return Source(
        id="trade-mail",
        name="Pet Age",
        url="imap://imap.gmail.com/digest",
        type=SourceType.IMAP,
        section="Newsletter",
        category_hint=Category.INDUSTRY,
        senders=tuple(senders),
    )


def test_a_newsletter_becomes_one_item_per_story():
    """A trade newsletter is fifteen stories in one message. Treating the email
    as a single item would let one dog-food story bury a coral one."""
    from dailydive import mailbox

    items = mailbox.items_from_message(_mail_source(), _newsletter())
    assert [i.url for i in items] == ["https://petage.com/coral-aquaculture-expands"]
    assert items[0].source_name == "Pet Age — Newsletter"


def test_mail_from_an_unlisted_sender_is_refused():
    """An inbox is an untrusted input: anyone who learns the address can mail
    it, and this is the only thing standing between that and a public page."""
    from dailydive import mailbox

    spam = _newsletter(sender="attacker@example.com")
    assert mailbox.items_from_message(_mail_source(), spam) == []


def test_an_empty_allowlist_refuses_everything():
    from dailydive import mailbox

    assert mailbox.items_from_message(_mail_source(senders=()), _newsletter()) == []


def test_navigation_and_boilerplate_are_not_stories():
    from dailydive import mailbox

    titles = [i.title for i in mailbox.items_from_message(_mail_source(), _newsletter())]
    assert not any("Unsubscribe" in t for t in titles)
    assert not any(t.strip() == "Read more" for t in titles)


def test_the_vocabulary_gate_drops_the_rest_of_the_pet_trade():
    """These are general pet-trade publications — mostly dogs, cats and retail.
    The gate is a cost control before the scorer, which is the real judge."""
    from dailydive import mailbox

    assert mailbox.looks_marine("Coral farms scale up aquaculture production")
    assert mailbox.looks_marine("New saltwater livestock shipment clears quarantine")
    assert not mailbox.looks_marine("Premium dog food sales climb for the eighth quarter")


def test_tracking_links_are_unwrapped_from_the_query_string():
    from dailydive import mailbox

    wrapped = "https://click.list-manage.com/track?u=abc&url=https%3A%2F%2Fpetage.com%2Fstory"
    assert mailbox.unwrap(wrapped) == "https://petage.com/story"


def test_an_unresolvable_tracking_link_drops_the_item():
    """Publishing the wrapper would push every reader through someone else's
    analytics and rot the day the campaign ends. Losing the item is better."""
    from dailydive import mailbox

    opaque = "https://petage.us1.list-manage.com/track/click?u=9f&id=7c2&e=aa"
    assert mailbox.unwrap(opaque) is None

    html = f'<a href="{opaque}">Coral aquaculture expands at marine facilities worldwide</a>'
    assert mailbox.items_from_message(_mail_source(), _newsletter(html=html)) == []


def test_a_plain_link_is_left_alone():
    from dailydive import mailbox

    assert mailbox.unwrap("https://petage.com/story") == "https://petage.com/story"


def test_mailcheck_refuses_to_read_a_whole_inbox():
    """It prints to a public Actions log. A mistyped label must not turn that
    into a dump of someone's personal mail."""
    from dailydive import mailbox

    for path in ("", "INBOX", "inbox", "[Gmail]/All Mail"):
        source = _mail_source().model_copy(update={"url": f"imap://imap.gmail.com/{path}"})
        out = mailbox.describe(source, user="x@example.com", password="unused", days=7)
        assert out.startswith("refusing to read"), path


def test_each_newsletter_source_speaks_for_exactly_one_outlet():
    """Attribution: an item must be credited to whoever wrote it. One shared
    "newsletters" source would have credited a Quality Marine announcement to
    something else, which is the one thing this project must never do."""
    mail = [
        s for s in config.load_sources(Path("sources.toml"), include_disabled=True)
        if s.type is SourceType.IMAP
    ]
    assert mail, "expected at least one configured newsletter source"
    for source in mail:
        assert len(source.senders) == 1, f"{source.id} mixes {len(source.senders)} senders"
        assert source.name, source.id


def test_an_item_that_already_ran_is_not_published_again(tmp_path):
    """store.known_uids existed, was tested, and was never called — so the
    archive recorded everything and suppressed nothing. A story from last
    week's issue would have run again this week while it sat inside the
    recency window."""
    db = tmp_path / "archive.sqlite3"
    first = item(url="https://reefbuilders.com/story/")

    with store.connect(db) as conn:
        assert store.known_uids(conn, [first.uid]) == set()
        store.record_items(conn, [first])

    # Second run, same story still inside the window.
    with store.connect(db) as conn:
        seen = store.known_uids(conn, [first.uid])
        fresh = [i for i in [first] if i.uid not in seen]
    assert fresh == [], "an item already in the archive must not reach a second issue"


def test_the_prompt_puts_deep_science_in_scope():
    """The earlier calibration scored a new species description at 0.2 "however
    good the science is", which emptied Livestock & Corals. Primary research in
    the reef subjects is now explicitly publishable."""
    raw = Path("prompts/score.system.md").read_text(encoding="utf-8")
    prompt = " ".join(raw.replace("*", "").split())
    assert "even as a primary research paper with no immediate application" in prompt
    assert "El Niño" in prompt
    # Public aquarium practice is the highest-interest science, per the editor.
    assert "public-aquarium husbandry item is 0.7 or better" in prompt
    # And the out-of-scope list still exists, or the section swamps the issue.
    assert "non-reef megafauna" in prompt


# ------------------------------------------------------------------ openalex
#
# The deep-science source. Its distinguishing property is that one response
# carries many outlets, so the attribution rules have to hold per item rather
# than per source — which is what most of these tests are about.

_OPENALEX_PAYLOAD = json.dumps(
    {
        "results": [
            {
                "display_name": "Thermal tolerance in Acropora under repeat bleaching",
                "publication_date": "2026-08-13",
                "doi": "https://doi.org/10.1007/s00338-026-12345-6",
                "primary_location": {
                    "source": {"display_name": "Coral Reefs"},
                    "landing_page_url": "https://link.springer.com/article/10.1007/s00338-026-12345-6",
                },
                "best_oa_location": {
                    "landing_page_url": "https://europepmc.org/article/MED/12345678"
                },
                "authorships": [
                    {"author": {"display_name": "A. Researcher"}},
                    {"author": {"display_name": "B. Coauthor"}},
                ],
                "abstract_inverted_index": {
                    "Corals": [0], "bleach": [1], "when": [2], "warm": [3]
                },
            },
            {
                # No venue: a preprint or an unregistered record. Uncreditable.
                "display_name": "A paper from nowhere in particular",
                "publication_date": "2026-08-12",
                "doi": "https://doi.org/10.9999/nowhere",
                "primary_location": {},
                "authorships": [],
            },
            {
                # No link at all. Nothing to send a reader to.
                "display_name": "A paper with no resolvable link",
                "publication_date": "2026-08-12",
                "primary_location": {"source": {"display_name": "Some Journal"}},
            },
        ]
    }
).encode()


def _openalex_source() -> Source:
    return Source(
        id="openalex-reef",
        name="OpenAlex",
        url="https://api.openalex.org/works?filter=from_publication_date:{since}",
        type=SourceType.OPENALEX,
        section="Journal",
    )


def test_a_paper_is_credited_to_its_journal_not_to_the_index():
    """The point of the whole source type. OpenAlex found the paper; Coral
    Reefs published it, and Coral Reefs is who gets the byline."""
    items = normalize.normalize(_openalex_source(), _OPENALEX_PAYLOAD)
    assert [i.source_name for i in items] == ["Coral Reefs"]
    assert "OpenAlex" not in items[0].source_name


def test_a_work_with_no_journal_is_dropped_rather_than_credited_loosely():
    titles = [i.title for i in normalize.normalize(_openalex_source(), _OPENALEX_PAYLOAD)]
    assert "A paper from nowhere in particular" not in titles


def test_a_work_with_no_link_is_dropped():
    titles = [i.title for i in normalize.normalize(_openalex_source(), _OPENALEX_PAYLOAD)]
    assert "A paper with no resolvable link" not in titles


def test_the_readable_open_access_link_beats_the_doi():
    """A DOI is the durable citation but can resolve to a paywall. The reader
    gets the version they can actually open; the DOI is kept alongside."""
    item = normalize.normalize(_openalex_source(), _OPENALEX_PAYLOAD)[0]
    assert item.url == "https://europepmc.org/article/MED/12345678"
    assert item.extra["doi"] == "https://doi.org/10.1007/s00338-026-12345-6"


def test_the_doi_is_used_when_there_is_no_open_access_copy():
    payload = json.dumps(
        {
            "results": [
                {
                    "display_name": "Paywalled but citable",
                    "publication_date": "2026-08-13",
                    "doi": "https://doi.org/10.1234/paywalled",
                    "primary_location": {"source": {"display_name": "Marine Biology"}},
                }
            ]
        }
    ).encode()
    assert normalize.normalize(_openalex_source(), payload)[0].url == "https://doi.org/10.1234/paywalled"


def test_long_author_lists_collapse_to_et_al():
    """Papers in this literature routinely carry dozens of authors."""
    item = normalize.normalize(_openalex_source(), _OPENALEX_PAYLOAD)[0]
    assert item.author == "A. Researcher et al."


def test_the_inverted_abstract_is_reconstructed_in_order():
    item = normalize.normalize(_openalex_source(), _OPENALEX_PAYLOAD)[0]
    assert item.raw_text == "Corals bleach when warm"


def test_an_openalex_error_yields_no_items_and_no_crash():
    body = json.dumps({"error": "Invalid query parameters", "message": "bad filter"}).encode()
    assert normalize.normalize(_openalex_source(), body) == []


def test_openalex_dates_are_timezone_aware():
    """A naive datetime would blow up the recency comparison against feeds."""
    item = normalize.normalize(_openalex_source(), _OPENALEX_PAYLOAD)[0]
    assert item.published_at.tzinfo is not None
    assert item.published_at == datetime(2026, 8, 13, tzinfo=UTC)


# ------------------------------------------------------- the {since} window

def test_a_since_placeholder_becomes_a_real_date():
    resolved = ingest.resolve_window(
        "https://api.openalex.org/works?filter=from_publication_date:{since}", days=21
    )
    expected = (datetime.now(UTC) - timedelta(days=21)).date().isoformat()
    assert resolved.endswith(expected)
    assert "{since}" not in resolved


def test_urls_without_the_placeholder_are_left_exactly_alone():
    """Every RSS source in the file goes through this, so it must be inert."""
    for url in ("https://reefbuilders.com/feed/", "https://bsky.app/profile/x/rss"):
        assert ingest.resolve_window(url) == url


def test_the_query_window_reaches_further_back_than_the_publishing_window():
    """An index lags the journal, so a query cut to exactly the publishing
    window would drop papers that are genuinely new to us."""
    assert ingest.QUERY_WINDOW_DAYS > normalize.DEFAULT_MAX_AGE_DAYS


def test_repository_deposits_are_dropped_rather_than_credited_to_the_repository():
    """The first live run credited a "Defensive Technical Disclosure" to
    "Zenodo (CERN European Organization for Nuclear Research)" — which names
    where the file sits, not who published it. type:article does not catch
    these; only the venue type does."""
    payload = json.dumps(
        {
            "results": [
                {
                    "display_name": "The Arrow: Open Ceramic System for Coral Restoration",
                    "publication_date": "2026-08-13",
                    "doi": "https://doi.org/10.5281/zenodo.21910906",
                    "primary_location": {
                        "source": {
                            "display_name": "Zenodo (CERN European Organization for Nuclear Research)",
                            "type": "repository",
                        }
                    },
                },
                {
                    "display_name": "A real paper in a real journal",
                    "publication_date": "2026-08-13",
                    "doi": "https://doi.org/10.1007/real",
                    "primary_location": {
                        "source": {"display_name": "Coral Reefs", "type": "journal"}
                    },
                },
            ]
        }
    ).encode()
    items = normalize.normalize(_openalex_source(), payload)
    assert [i.source_name for i in items] == ["Coral Reefs"]


def test_a_work_with_no_venue_type_is_still_kept():
    """The venue check drops known repositories, not everything it can't
    classify — an absent type is missing metadata, not a deposit."""
    payload = json.dumps(
        {
            "results": [
                {
                    "display_name": "Typeless but published",
                    "publication_date": "2026-08-13",
                    "doi": "https://doi.org/10.1234/typeless",
                    "primary_location": {"source": {"display_name": "Marine Biology"}},
                }
            ]
        }
    ).encode()
    assert normalize.normalize(_openalex_source(), payload)[0].source_name == "Marine Biology"


def test_the_prompt_refuses_relevance_built_by_bridging():
    """The first scored run under the subject test kept marine-turtle poaching
    (0.60) and farmed-salmon feed economics (0.55) — both explicitly on the
    out-of-scope list — by reasoning to reef relevance rather than being about
    reefs. The tell was the verb, so the prompt now names the verbs."""
    raw = Path("prompts/score.system.md").read_text(encoding="utf-8")
    prompt = " ".join(raw.replace("*", "").split())
    assert "The subject is the subject, not what it can be connected to" in prompt
    assert "illustrates" in prompt
    assert "you have built a bridge instead of finding a subject" in prompt
    # And the two species/industries that slipped through are named outright.
    assert "marine turtles" in prompt
    assert "aquaculture of food species" in prompt


# ------------------------------------------------------------------- masthead

def test_the_publication_name_appears_nowhere_but_brand_py():
    """A rename must be one edit. "Daily Dive" was hardcoded in six places
    despite brand.py existing to prevent exactly that, which meant renaming the
    publication was a search-and-replace across templates and Python."""
    # Every template, not just the issue page. The email template was added
    # later and put the name in its header comment on the first try, which is
    # how this drifts: prose in a file nobody re-reads after a rename.
    for template in sorted(render.TEMPLATE_DIR.glob("*.j2")):
        assert brand.PUBLICATION not in template.read_text(encoding="utf-8"), template.name
    for module in ("render.py", "deliver.py"):
        source = Path("dailydive") / module
        assert brand.PUBLICATION not in source.read_text(encoding="utf-8"), module


def test_renaming_the_publication_changes_the_page(monkeypatch):
    """The end the previous test is a means to: change the constant, and the
    title, masthead, greeting and footer all follow."""
    monkeypatch.setattr(brand, "PUBLICATION", "Reef Weekly")
    html = render.render_issue(Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item()]))
    assert "Reef Weekly" in html
    assert "Daily Dive" not in html


def test_the_page_never_claims_a_cadence_it_does_not_keep():
    """The masthead said "daily" on a weekly for weeks before anyone noticed.

    The name now carries the cadence — "Weekly Dive" — which is better copy and
    a worse guarantee, because a name is free text. So the check is that the
    name, the CADENCE constant and the actual cron all agree. Change the
    schedule without changing the name and this fails, which is the point."""
    assert brand.CADENCE in brand.DESCRIPTION

    words = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
    claimed = {w for w in words if w in brand.PUBLICATION.lower()}
    assert claimed <= {brand.CADENCE}, (
        f"{brand.PUBLICATION!r} claims {claimed}, but CADENCE is {brand.CADENCE!r}"
    )

    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")
    cron = re.search(r'cron:\s*"([^"]+)"', workflow).group(1)
    minute, hour, dom, month, dow = cron.split()
    if brand.CADENCE == "weekly":
        # A single day-of-week, a fixed hour, and every day-of-month: that is
        # once a week and nothing else.
        assert dow not in ("*", "?") and "," not in dow and "-" not in dow, cron
        assert dom == "*" and month == "*", cron
        assert hour.isdigit() and minute.isdigit(), cron


def test_banner_dimensions_are_read_from_the_file_not_typed_in():
    """The template used to hardcode 954x202. Replacement artwork of any other
    size would render at the old aspect ratio and jump on load.

    This asserts the reported size matches the file, NOT a fixed pair. An
    earlier version hardcoded the then-current dimensions and duly broke the
    moment new artwork landed — a test that makes a drop-in not a drop-in is
    testing the opposite of the property it is named for.
    """
    found = render.find_header_image(Path("site"))
    assert found is not None, "site/assets/masthead.png should be committed"
    path, width, height = found
    assert path == "assets/masthead.png"

    raw = (Path("site") / path).read_bytes()
    actual = struct.unpack(">II", raw[16:24])
    assert (width, height) == actual


def test_a_banner_of_a_different_size_reports_its_own_dimensions(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/masthead.png").write_bytes(_png(1908, 404))
    assert render.find_header_image(tmp_path)[1:] == (1908, 404)


def test_an_unreadable_banner_still_renders_without_dimensions(tmp_path):
    """A non-PNG or a truncated file loses the layout reservation, not the
    image — CSS still sizes it."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/masthead.webp").write_bytes(b"RIFF????WEBP")
    path, width, height = render.find_header_image(tmp_path)
    assert path == "assets/masthead.webp"
    assert (width, height) == (None, None)
    html = render.render_issue(
        Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item()]),
        header=render.find_header_image(tmp_path),
    )
    assert "masthead.webp" in html
    assert "width=" not in html.split("</h1>")[0].split('<h1 class="wordmark">')[-1]


# -------------------------------------------------------------- link previews

def test_the_front_page_and_the_permalink_do_not_compete_as_duplicates(tmp_path):
    """Both carry the same issue. Without a canonical each week's issue would
    exist at two URLs, which splits whatever ranking either would have had."""
    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item()])
    render.write_issue(issue, tmp_path)

    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    dated = (tmp_path / "issues/2026-08-21.html").read_text(encoding="utf-8")
    assert f'rel="canonical" href="{brand.SITE_URL}/"' in index
    assert f'rel="canonical" href="{brand.SITE_URL}/issues/2026-08-21.html"' in dated


def test_a_link_preview_says_what_is_in_the_issue():
    """A forwarded link is how this spreads. "12 items from 7 outlets, led by
    ..." helps someone decide; the standing tagline repeats the title."""
    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item(), item(url="https://b.example/2")])
    desc = render.og_description(issue)
    assert "2 items from" in desc
    assert "led by" in desc
    assert desc in render.render_issue(issue)


def test_an_empty_issue_falls_back_to_the_standing_description():
    assert render.og_description(Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[])) == brand.DESCRIPTION


def test_a_very_long_headline_is_trimmed_in_the_preview():
    long_title = "A headline about coral " * 12
    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item(title=long_title)])
    desc = render.og_description(issue)
    assert len(desc) < 160  # link previews truncate past roughly this
    assert "…" in desc


def test_the_preview_image_is_an_absolute_url(tmp_path):
    """Relative og:image paths are ignored by most unfurlers."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/masthead.png").write_bytes(_png(1908, 404))
    html = render.render_issue(
        Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item()]),
        header=render.find_header_image(tmp_path, depth=1),
    )
    assert f'content="{brand.SITE_URL}/assets/masthead.png"' in html
    assert 'content="../assets/masthead.png"' not in html
    assert 'name="twitter:card" content="summary_large_image"' in html


# ------------------------------------------------------------------ publishing

def _workflow() -> dict:
    yaml = pytest.importorskip("yaml", reason="pyyaml is a local convenience, not a dependency")
    return yaml.safe_load(Path(".github/workflows/daily.yml").read_text(encoding="utf-8"))


def test_publishing_is_gated_on_a_full_successful_run():
    """Pages replaces the whole site on every deploy, so publishing a --limit 5
    test run would swap a real issue for a test — silently, until someone
    noticed the following Friday."""
    steps = {s.get("name"): s for s in _workflow()["jobs"]["build"]["steps"]}
    gate = steps["Decide whether this run publishes"]
    script = gate["run"]
    assert "the build did not succeed" in script
    assert "IN_SOURCE" in script and "IN_LIMIT" in script and "IN_PROBE_URLS" in script

    for name in ("Commit the issue and the archive", "Package the site", "Deploy to Pages"):
        assert steps[name]["if"] == "steps.gate.outputs.publish == 'true'", name


def test_the_workflow_can_write_what_it_needs_and_no_more():
    """Each grant earns its place: contents commits the issue and the archive,
    pages + id-token deploy, issues reads the pick bucket and answers it."""
    perms = _workflow()["permissions"]
    assert perms == {
        "contents": "write",
        "pages": "write",
        "id-token": "write",
        "issues": "write",
    }


def test_deploys_do_not_cancel_each_other():
    """A half-written Pages deploy is a broken site, which is worse than a late
    one."""
    assert _workflow()["concurrency"]["cancel-in-progress"] is False


def test_the_archive_database_is_committed_back():
    """The dedupe memory and the HTTP cache both live in this file. Every
    scheduled run started from an empty one until the commit step existed, so
    known_uids had nothing to compare against."""
    steps = {s.get("name"): s for s in _workflow()["jobs"]["build"]["steps"]}
    assert "dailydive.sqlite3" in steps["Commit the issue and the archive"]["run"]
    ignored = Path(".gitignore").read_text(encoding="utf-8")
    assert "dailydive.sqlite3" not in ignored
    assert "\nsite/issues/\n" not in ignored


# ------------------------------------------------------------ community filing

def test_channels_and_forums_are_community_by_their_type():
    """A tank tour is a tank tour whatever it is about. Left to the text, the
    model files these under whichever subject the title mentions, scattering
    the community across five sections."""
    for kind in (SourceType.YOUTUBE, SourceType.YOUTUBE_API, SourceType.XENFORO, SourceType.REDDIT):
        s = Source(id="x", name="X", url="https://x.invalid/f", type=kind)
        assert s.is_community, kind


def test_a_hobbyist_site_opts_in_because_its_type_cannot_tell():
    """Reefs.com is an ordinary WordPress feed, shaped exactly like the trade
    press. Only the flag distinguishes them."""
    plain = Source(id="a", name="A", url="https://a.invalid/feed", type=SourceType.WORDPRESS)
    hobby = Source(id="b", name="B", url="https://b.invalid/feed", type=SourceType.WORDPRESS, community=True)
    assert not plain.is_community
    assert hobby.is_community


def test_the_configured_community_sources_are_the_expected_ones():
    ids = {s.id for s in config.load_sources(Path("sources.toml")) if s.is_community}
    assert "reefs" in ids
    assert all(i.startswith("yt-") for i in ids - {"reefs"}), ids


def test_a_community_source_overrides_the_models_category():
    """The source knows better than a title and a description here."""
    from dailydive.score import ItemScore, apply_scores

    vid = item(url="https://youtube.invalid/watch?v=1", source_id="yt-brs")
    scored = {vid.uid: ItemScore(uid=vid.uid, relevance=0.8, category=Category.HUSBANDRY,
                                 is_promo=False, gist="A tank tour that mentions alkalinity.")}
    out = apply_scores([vid], scored, community_sources=frozenset({"yt-brs"}))
    assert out[0].category_hint is Category.COMMUNITY


def test_a_non_community_source_keeps_the_models_category():
    from dailydive.score import ItemScore, apply_scores

    art = item(url="https://reefbuilders.invalid/a", source_id="reefbuilders")
    scored = {art.uid: ItemScore(uid=art.uid, relevance=0.8, category=Category.HUSBANDRY,
                                 is_promo=False, gist="A husbandry article.")}
    out = apply_scores([art], scored, community_sources=frozenset({"yt-brs"}))
    assert out[0].category_hint is Category.HUSBANDRY


# -------------------------------------------------------------------- preview

def test_the_preview_covers_every_section():
    """The point of the fixture. A layout change judged against a partial issue
    is a layout change judged against the easy case — the real issue that seeded
    this had no Events and nothing uncategorized."""
    from dailydive import preview

    issue = preview.load_issue()
    titles = [t for t, _, _ in render.group_by_category(issue)]
    assert titles[0] == Category.COMMUNITY.value  # order is part of the design
    for category in Category:
        assert category.value in titles, category
    assert "Elsewhere" in titles


def test_the_preview_exercises_the_item_level_badges():
    """Runtime, beat and near-duplicate badges appear on a minority of items,
    which is exactly why a preview without them is misleading."""
    from dailydive import preview

    extras = [i.extra for i in preview.load_issue().items]
    assert any("duration_s" in e for e in extras)
    assert any("beat" in e for e in extras)
    assert any("similar" in e for e in extras)
    assert sum("gist" in e for e in extras) > 10


def test_the_preview_needs_no_network_and_no_key(tmp_path, monkeypatch):
    """It has to be free, or it is not a design loop."""
    from dailydive import preview

    def refuse(*a, **k):
        raise AssertionError("preview must not open a socket")

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = preview.write_preview(tmp_path, site_dir=Path("site"))
    assert path.read_text(encoding="utf-8").lstrip().startswith("<!doctype html>")


def test_the_preview_inlines_the_masthead_so_it_renders_anywhere(tmp_path):
    """It gets looked at in a chat window, not in the directory it was written
    to. A broken image reads as a broken design."""
    from dailydive import preview

    html = preview.write_preview(tmp_path, site_dir=Path("site")).read_text(encoding="utf-8")
    assert 'src="data:image/png;base64,' in html
    assert 'src="assets/masthead.png"' not in html
    # The og:image must stay an absolute live URL — no unfurler takes a data URI.
    assert f'content="{brand.SITE_URL}/assets/masthead.png"' in html


def test_the_synthetic_preview_items_are_labelled_as_such():
    """Two items are invented to cover empty sections. Nothing invented should
    ever be mistakable for reporting, even in a preview."""
    from dailydive import preview

    invented = [i for i in preview.load_issue().items if "example.invalid" in i.url]
    assert invented, "the fixture should carry the coverage items"
    for i in invented:
        assert i.title.startswith("[SYNTHETIC]"), i.title


def test_the_artifact_form_drops_the_document_wrapper():
    """An artifact supplies its own doctype, head and body. Publishing a full
    document nests one inside another."""
    import tempfile
    from dailydive import preview

    with tempfile.TemporaryDirectory() as tmp:
        html = preview.artifact_html(Path(tmp), site_dir=Path("site"))
    assert "<!doctype" not in html.lower()
    assert "<html" not in html.lower() and "<body" not in html.lower()
    assert html.startswith("<title>")
    assert "<style>" in html and 'class="wrap"' in html


def test_the_artifact_form_drops_tags_that_name_the_live_site():
    """Canonical and OpenGraph tags point at theloneaquarist.com. On a staging
    page they are simply wrong."""
    import tempfile
    from dailydive import preview

    with tempfile.TemporaryDirectory() as tmp:
        html = preview.artifact_html(Path(tmp), site_dir=Path("site"))
    assert 'rel="canonical"' not in html
    assert "og:url" not in html


def test_the_artifact_paints_its_own_ground():
    """It renders inside a host that paints the viewer's theme behind it. A
    transparent body would show dark grey through a page designed on white."""
    import tempfile
    from dailydive import preview

    with tempfile.TemporaryDirectory() as tmp:
        html = preview.artifact_html(Path(tmp), site_dir=Path("site"))
    assert re.search(r"body\s*\{[^}]*background:", html)


def test_the_sticky_header_is_not_trapped_in_a_scroll_container():
    """overflow:hidden on section.card makes the card the sticky header's
    scroll container, so the header sticks inside a box it already fills and
    the effect dies silently. Verified in Chromium: with the overflow the
    header scrolled to -200px; without it, it held at 0."""
    css = (render.TEMPLATE_DIR / "issue.html.j2").read_text(encoding="utf-8")
    card_rule = re.search(r"section\.card \{[^}]*\}", css)
    assert card_rule, "section.card rule moved — re-check the sticky header"
    assert "overflow" not in card_rule.group(0), card_rule.group(0)


def test_sticky_positioning_is_not_gated_behind_reduced_motion():
    """Sticky is not animation — nothing accelerates or parallaxes. Gating it
    there switched the header off for everyone with Reduce Motion enabled,
    which on iOS is a great many people."""
    css = (render.TEMPLATE_DIR / "issue.html.j2").read_text(encoding="utf-8")
    for block in re.findall(r"@media \(prefers-reduced-motion[^{]*\{(.*?)\n  \}", css, re.S):
        assert "sticky" not in block, block


# ----------------------------------------------------------------- editor picks

def _pick_body(**over) -> str:
    f = {"Headline": "Ecotech teases a Vectra successor",
         "Link": "https://www.reef2reef.com/threads/vectra.1/",
         "Outlet": "Reef2Reef", "Category": "Industry & Products",
         "Industry beat": "Product",
         "Why it matters": "Staff hinted at a successor in a customer thread, without dates.",
         "Published": "2026-08-18"}
    f.update(over)
    return "\n".join(f"### {k}\n\n{v}\n" for k, v in f.items())


def test_a_pick_becomes_an_item():
    from dailydive import picks

    item = picks.to_item(_pick_body(), number=7)
    assert item.source_name == "Reef2Reef"
    assert item.category_hint is Category.INDUSTRY
    assert item.extra["beat"] == "Product"
    assert item.extra["pick_issue"] == "7"
    assert picks.is_pick(item)


def test_a_pick_never_carries_an_author():
    """Forum members didn't ask to be published, and the mistake isn't undoable
    once it's on a public page and in git history."""
    from dailydive import picks

    item = picks.to_item(_pick_body(**{"Outlet": "Reef2Reef"}), number=1)
    assert item.author is None
    # And there is no field that could supply one.
    assert "author" not in picks.parse_body(_pick_body())


def test_a_blank_optional_field_is_treated_as_empty():
    """GitHub writes _No response_ into an issue-form field left blank."""
    from dailydive import picks

    item = picks.to_item(_pick_body(**{"Why it matters": "_No response_",
                                       "Industry beat": "_No response_"}), number=1)
    assert "gist" not in item.extra and "beat" not in item.extra


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"Headline": ""}, "missing a headline"),
        ({"Link": ""}, "missing a link"),
        ({"Outlet": ""}, "missing an outlet"),
        ({"Link": "reef2reef.com/threads/1"}, "isn't a usable link"),
        ({"Category": "Gossip"}, "don't recognise the category"),
        ({"Why it matters": "word " * 41}, "ceiling is 40"),
        ({"Published": "next tuesday"}, "couldn't read"),
    ],
)
def test_a_bad_pick_is_rejected_with_a_reason_worth_reading(override, expected):
    """The reason goes on the issue as a comment, so it has to be a sentence a
    person can act on rather than a stack trace."""
    from dailydive import picks

    with pytest.raises(picks.PickError) as exc:
        picks.to_item(_pick_body(**override), number=1)
    assert expected in str(exc.value)


class _FakeBucket:
    """Stands in for the GitHub API. The decisions live outside the network."""

    def __init__(self, issues):
        self.issues = issues
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []

    def open_picks(self):
        return [i for i in self.issues if (i["user"]["login"].lower() in {"muxwilliams"})]

    def comment(self, number, message):
        self.comments.append((number, message))

    def close(self, number, message):
        self.comments.append((number, message))
        self.closed.append(number)


def test_only_allowlisted_authors_can_file_a_pick():
    """The repo is public — anyone can open an issue on it. Without this, a
    stranger puts a link on the front page."""
    from dailydive import picks

    bucket = _FakeBucket([
        {"number": 1, "body": _pick_body(), "user": {"login": "MUXWilliams"}},
        {"number": 2, "body": _pick_body(**{"Headline": "Buy my thing"}),
         "user": {"login": "somebody-else"}},
    ])
    items, rejected = picks.collect(bucket, published_uids=set())
    assert [i.title for i in items] == ["Ecotech teases a Vectra successor"]
    # The stranger gets no reply at all — silence tells them nothing.
    assert rejected == [] and bucket.comments == []


def test_a_pick_for_something_already_published_is_rejected():
    from dailydive import picks

    item = picks.to_item(_pick_body(), number=1)
    bucket = _FakeBucket([{"number": 1, "body": _pick_body(), "user": {"login": "muxwilliams"}}])
    items, rejected = picks.collect(bucket, published_uids={item.uid})
    assert items == []
    assert "already ran" in rejected[0][1]


def test_published_is_recorded_separately_from_seen(tmp_path):
    """items is a SEEN log — everything fetched, including what scoring dropped.
    Asking it "did we publish this?" rejects picks for stories a machine
    glanced at and discarded."""
    db = tmp_path / "t.sqlite3"
    seen_only = item(url="https://a.invalid/never-ran")
    ran = item(url="https://a.invalid/ran")
    with store.connect(db) as conn:
        store.record_items(conn, [seen_only, ran])
        store.record_published(conn, [ran], datetime(2026, 8, 21, tzinfo=UTC))
    with store.connect(db) as conn:
        assert store.published_uids(conn) == {ran.uid}
        assert store.known_uids(conn, [seen_only.uid]) == {seen_only.uid}


def test_a_pick_survives_collapse_and_takes_the_crawlers_coverage_with_it():
    """The collision that actually happens: same story, two outlets, different
    URLs. collapse_similar keeps the first of each group, so a pick placed
    first survives and the crawled version becomes its "+1 similar" credit."""
    from dailydive import picks

    pick = picks.to_item(_pick_body(**{
        "Headline": "Ecotech teases a Vectra successor at MACNA"}), number=3)
    crawled = item(url="https://reefbuilders.invalid/vectra",
                   title="Ecotech teases a Vectra successor at MACNA")

    out = normalize.collapse_similar([pick] + [crawled])
    assert len(out) == 1
    assert picks.is_pick(out[0]), "the pick must be the survivor, not the crawled copy"
    assert out[0].extra["similar"] == "1"


def test_without_the_ordering_the_crawler_would_win():
    """States the dependency outright: pick-survives is ordering, not magic.
    If picks ever stop being prepended, this is the test that explains why the
    behaviour changed."""
    from dailydive import picks

    pick = picks.to_item(_pick_body(**{
        "Headline": "Ecotech teases a Vectra successor at MACNA"}), number=3)
    crawled = item(url="https://reefbuilders.invalid/vectra",
                   title="Ecotech teases a Vectra successor at MACNA")

    out = normalize.collapse_similar([crawled] + [pick])
    assert len(out) == 1
    assert not picks.is_pick(out[0])


def test_the_plain_text_issue_carries_the_byline():
    """The HTML credit line has it and the text form dropped it. It is how a
    run reports what a publisher actually calls itself, which is the check
    against a configured name typed from memory."""
    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC),
                  items=[item(author="Melev's Reef")])
    assert "Melev's Reef" in render.as_text(issue)


def test_a_missing_byline_leaves_no_dangling_separator():
    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item(author=None)])
    assert " ·  · " not in render.as_text(issue)


def test_the_allowlist_covers_the_account_that_files_not_just_the_repo_owner():
    """The repo is under MUXWilliams; issues are filed by muxxworx. Assuming
    they were the same login made the first real pick vanish silently."""
    from dailydive import picks

    assert "muxxworx" in picks.AUTHORS
    bucket = _FakeBucket([{"number": 1, "body": _pick_body(), "user": {"login": "muxxworx"}}])
    bucket.open_picks = lambda: [
        i for i in bucket.issues if i["user"]["login"].lower() in picks.AUTHORS
    ]
    items, _ = picks.collect(bucket, published_uids=set())
    assert len(items) == 1


def _run_args(**over):
    base = dict(offline=False, source=None, limit=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_a_partial_run_does_not_claim_to_have_published():
    """The workflow refuses to deploy a --source or --limit build. Without the
    same test in the CLI, a five-item test run marks those items published and
    closes the pick issues that fed it — pointing at a page nobody served."""
    assert cli._is_publishing_run(_run_args())
    assert not cli._is_publishing_run(_run_args(source=["reefbuilders"]))
    assert not cli._is_publishing_run(_run_args(limit=5))
    assert not cli._is_publishing_run(_run_args(offline=True))


def test_the_cli_gate_and_the_workflow_gate_test_the_same_things():
    """They are two implementations of one rule, and drift between them is
    silent — the workflow would skip the deploy while the CLI recorded it."""
    steps = {s.get("name"): s for s in _workflow()["jobs"]["build"]["steps"]}
    script = steps["Decide whether this run publishes"]["run"]
    for token in ("IN_SOURCE", "IN_LIMIT"):
        assert token in script
    source = Path("dailydive/cli.py").read_text(encoding="utf-8")
    gate = re.search(r"def _is_publishing_run.*?return [^\n]+", source, re.S).group(0)
    assert "args.source" in gate and "args.limit" in gate and "args.offline" in gate


# --------------------------------------------------------------------- archive

def _issue_on(day, lead="A lead story", n=3):
    from dailydive.models import Item as I
    titles = [lead] + [f"Story {i}" for i in range(n - 1)]
    return Issue(date=datetime(2026, 8, day, tzinfo=UTC), items=[
        I(source_id="s", source_name=f"Outlet {i % 2}", title=t,
          url=f"https://ex.invalid/{day}/{i}", published_at=datetime(2026, 8, day, tzinfo=UTC))
        for i, t in enumerate(titles)])


def test_the_archive_lists_issues_newest_first(tmp_path):
    from dailydive import archive

    archive.record(tmp_path, _issue_on(15))
    entries = archive.record(tmp_path, _issue_on(16))
    assert [e["date"] for e in entries] == ["2026-08-16", "2026-08-15"]


def test_rerunning_a_day_replaces_its_entry_rather_than_duplicating_it(tmp_path):
    """A re-run overwrites the dated permalink, so the index has to describe the
    page that is actually there."""
    from dailydive import archive

    archive.record(tmp_path, _issue_on(16, lead="First attempt", n=2))
    entries = archive.record(tmp_path, _issue_on(16, lead="Second attempt", n=9))
    assert len(entries) == 1
    assert entries[0]["lead"] == "Second attempt"
    assert entries[0]["items"] == 9


def test_a_corrupt_index_costs_the_listing_not_the_issue(tmp_path):
    """Losing the archive page is recoverable. Failing the run is not."""
    from dailydive import archive

    (tmp_path / "issues").mkdir()
    (tmp_path / archive.INDEX).write_text("{not json at all", encoding="utf-8")
    entries = archive.record(tmp_path, _issue_on(16))
    assert len(entries) == 1


def test_every_issue_page_links_to_the_archive_from_its_own_depth(tmp_path):
    """index.html and issues/*.html sit at different depths; one relative path
    cannot serve both."""
    from dailydive import archive

    issue = _issue_on(16)
    render.write_issue(issue, tmp_path)
    archive.record(tmp_path, issue)
    assert archive.write_page(tmp_path) is not None

    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    dated = (tmp_path / "issues/2026-08-16.html").read_text(encoding="utf-8")
    assert 'href="archive.html"' in index
    assert 'href="../archive.html"' in dated


def test_the_archive_page_names_each_issue_by_date_and_lead(tmp_path):
    from dailydive import archive

    archive.record(tmp_path, _issue_on(16, lead="Repeated bleaching in the Florida Keys", n=6))
    html = archive.write_page(tmp_path).read_text(encoding="utf-8")
    assert "Sunday, August 16, 2026" in html
    assert "Repeated bleaching in the Florida Keys" in html
    assert "6 items" in html
    assert 'href="issues/2026-08-16.html"' in html


def test_no_archive_page_when_there_is_nothing_to_list(tmp_path):
    from dailydive import archive

    assert archive.write_page(tmp_path) is None


def test_the_archive_names_the_story_a_reader_sees_first(tmp_path):
    """Not the highest-scoring one. Those diverged when Community moved to the
    top of the stack, and naming a story the reader must scroll to find
    describes a different page than the one the line links to."""
    from dailydive import archive
    from dailydive.models import Item as I

    top_scorer = I(source_id="s", source_name="Journal", title="A high-scoring paper",
                   url="https://ex.invalid/paper", published_at=datetime(2026, 8, 16, tzinfo=UTC),
                   category_hint=Category.WILD_REEFS)
    what_you_see = I(source_id="s", source_name="Reef2Reef", title="What leads the page",
                     url="https://ex.invalid/thread", published_at=datetime(2026, 8, 16, tzinfo=UTC),
                     category_hint=Category.COMMUNITY)
    # Relevance order puts the paper first; section order puts Community first.
    issue = Issue(date=datetime(2026, 8, 16, tzinfo=UTC), items=[top_scorer, what_you_see])

    assert render.group_by_category(issue)[0][0] == Category.COMMUNITY.value
    assert archive.entry_for(issue)["lead"] == "What leads the page"


def test_the_preview_carries_the_same_chrome_as_a_real_front_page(tmp_path):
    """A preview missing a piece of the live page is the one thing a preview
    must never be."""
    from dailydive import preview

    html = preview.write_preview(tmp_path, site_dir=Path("site")).read_text(encoding="utf-8")
    assert 'href="archive.html"' in html
    assert "back issues" in html


# ------------------------------------------------------------ session briefing

def test_the_briefing_names_the_rules_that_must_not_be_broken():
    """CLAUDE.md is what a fresh session reads instead of a chat history. If a
    hard-won invariant isn't in it, the next session doesn't know it exists."""
    brief = Path("CLAUDE.md").read_text(encoding="utf-8")
    for rule in [
        "credits and links its source",       # attribution
        "never forge a User-Agent",           # how blocked sources are handled
        "Never publish anything invented",    # the fixture page incident
        "Picks never carry an author",        # the privacy rule
        "allowlists",                         # IMAP + issue bucket
        "environment only",                   # secrets
    ]:
        assert rule in brief, rule


def test_the_briefing_points_at_files_that_exist():
    """A briefing that sends the next session to a moved file is worse than
    none — it spends their trust before they find out."""
    brief = Path("CLAUDE.md").read_text(encoding="utf-8")
    for path in re.findall(r"`([a-zA-Z0-9_./-]+\.(?:py|md|toml|yml|json|sqlite3))`", brief):
        candidate = Path(path)
        if candidate.suffix == ".sqlite3":
            continue  # generated by a run, absent on a clean checkout
        assert candidate.exists() or (Path("dailydive") / path).exists(), path


# ------------------------------------------------------------- about page

def test_about_page_names_the_publication_from_brand(tmp_path):
    """The about page was hand-written static HTML and spent weeks calling the
    publication by its old name and its old cadence, because nothing tied the
    words on it to brand.py. It renders from a template now; this is the test
    that keeps it that way."""
    html = render.write_about(tmp_path).read_text(encoding="utf-8")
    assert brand.PUBLICATION in html
    assert "Daily Dive" not in html
    assert "each morning" not in html
    assert f"each {brand.CADENCE_NOUN}" in html


def test_about_page_keeps_the_crawler_name(tmp_path):
    """DailyDiveBot is deliberately NOT renamed with the publication: publishers
    may have allowlisted the string. A well-meaning tidy-up here would make us
    an unrecognised agent overnight."""
    html = render.write_about(tmp_path).read_text(encoding="utf-8")
    assert brand.BOT_NAME in html
    assert brand.CONTACT_EMAIL in html


def test_about_page_is_single_theme_like_the_rest_of_the_site(tmp_path):
    html = render.write_about(tmp_path).read_text(encoding="utf-8")
    assert "prefers-color-scheme" not in html


# --------------------------------------------------------- resource section

def video(vid: str, title: str, relevance: str, **kw) -> Item:
    extra = {"video_id": vid, "relevance": relevance, **kw.pop("extra", {})}
    return item(
        title=title,
        url=f"https://www.youtube.com/watch?v={vid}",
        source_name="A Channel",
        category_hint=Category.COMMUNITY,
        extra=extra,
        **kw,
    )


def test_resource_prefers_an_instructional_title_over_a_higher_score():
    """The section is called Resource, so what fills it should be something you
    would come back to — not whichever video happened to score highest."""
    news = video("aaa", "New gyre pump announced at MACNA", "0.95")
    howto = video("bbb", "How to dose alkalinity without crashing your tank", "0.60")
    issue = Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[news, howto])
    assert render.pick_resource(issue) is howto


def test_resource_falls_back_to_the_best_video_when_nothing_is_instructional():
    """An empty section in a fixed layout reads as a bug to a reader who does
    not know the rule."""
    low = video("aaa", "Reef tour of a 300 gallon system", "0.50")
    high = video("bbb", "MACNA floor walkthrough", "0.88")
    issue = Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[low, high])
    assert render.pick_resource(issue) is high


def test_resource_prefers_a_pick_over_a_higher_scoring_keyword_match():
    """Picks outrank the model everywhere else in this pipeline."""
    howto = video("aaa", "How to frag a torch coral", "0.95")
    chosen = video("bbb", "Reef tour", "0.40", extra={"pick": "1"})
    issue = Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[howto, chosen])
    assert render.pick_resource(issue) is chosen


def test_resource_is_none_when_the_issue_has_no_video():
    issue = Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[item()])
    assert render.pick_resource(issue) is None


def test_resource_keywords_match_words_not_substrings():
    """A bare substring test files "multiple" under tips and "misuse" under
    mistakes. Both have appeared in real reef headlines."""
    for miss in ["Multiple corals arrived today", "Misuse of a refractometer", "Stripped screws"]:
        assert not render.RESOURCE_WORDS.search(miss), miss
    for hit in ["How to dose", "How-to: dosing", "Three tips", "One trick", "A common mistake"]:
        assert render.RESOURCE_WORDS.search(hit), hit


def test_the_resource_video_appears_exactly_once_on_the_page():
    """It is promoted out of its category, not copied into a second section."""
    howto = video("bbb", "How to dose alkalinity", "0.60")
    issue = Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[video("aaa", "Reef tour", "0.9"), howto])
    html = render.render_issue(issue)
    assert html.count("watch?v=bbb") == 1
    assert "<span>Resource</span>" in html


def test_the_archive_lead_ignores_the_promoted_video():
    """The archive line names the first story a reader sees. The Resource video
    renders at the foot of the page, so naming it would describe a story they
    have to scroll past everything else to reach."""
    from dailydive import archive

    howto = video("bbb", "How to dose alkalinity", "0.99")
    other = item(title="A community build thread", category_hint=Category.COMMUNITY)
    issue = Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[howto, other])
    assert archive.entry_for(issue)["lead"] == "A community build thread"


def test_an_issue_with_a_resource_but_no_thumbnail_still_renders():
    """An unreachable CDN costs the picture, never the issue."""
    issue = Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[video("bbb", "How to dose", "0.6")])
    html = render.render_issue(issue, thumb=None)
    assert "<span>Resource</span>" in html
    assert "<img" not in html.split('class="card sec-resource', 1)[1]


# -------------------------------------------------------------- thumbnails

def _jpeg(width: int, height: int, pad: int = 0) -> bytes:
    """Just enough JPEG for the size reader: SOI, a skipped APP0, then an SOF0."""
    app0 = b"\xff\xe0" + struct.pack(">H", 2 + pad) + b"\x00" * pad
    sof = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof


def test_jpeg_dimensions_are_read_from_the_file():
    from dailydive import thumbs

    assert thumbs.jpeg_size(_jpeg(1280, 720)) == (1280, 720)


def test_a_404_html_body_is_not_mistaken_for_a_thumbnail():
    from dailydive import thumbs

    assert thumbs.jpeg_size(b"<!doctype html><title>404</title>") is None


def test_a_truncated_jpeg_is_rejected_rather_than_guessed():
    from dailydive import thumbs

    assert thumbs.jpeg_size(_jpeg(1280, 720)[:6]) is None


def test_youtubes_grey_placeholder_is_rejected(tmp_path):
    """A missing maxresdefault comes back as a ~1KB 120x90 grey rectangle with a
    200, so "did we get a JPEG?" is not a sufficient test."""
    from dailydive import thumbs

    placeholder = _jpeg(120, 90, pad=800)
    real = _jpeg(1280, 720, pad=8000)

    def handler(request: httpx.Request) -> httpx.Response:
        body = placeholder if "maxresdefault" in str(request.url) else real
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = thumbs.fetch("abc123", tmp_path, client=client)
    assert got == ("assets/thumbs/abc123.jpg", 1280, 720)
    assert (tmp_path / "assets/thumbs/abc123.jpg").read_bytes() == real


def test_a_video_id_that_could_escape_the_path_is_refused(tmp_path):
    """The id arrives from a feed. It becomes both a filename and a URL path
    segment, so anything that could climb out of either is refused."""
    from dailydive import thumbs

    assert thumbs.fetch("../../etc/passwd", tmp_path) is None
    assert thumbs.fetch("", tmp_path) is None


def test_a_byline_that_repeats_the_outlet_is_not_printed_twice():
    """The YouTube API sets author to the channel title, which is already the
    source name — the credit line read "ReefDudes · ReefDudes"."""
    same = item(source_name="ReefDudes", author="ReefDudes", category_hint=Category.COMMUNITY)
    html = render.render_issue(Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[same]))
    # Scoped to the credit line — the footer's outlet list names it again, which
    # is correct there.
    credit = re.search(r'<p class="credit">(.*?)</p>', html, re.S).group(1)
    assert credit.count("ReefDudes") == 1

    named = item(source_name="Reef Builders", author="Jake Adams", category_hint=Category.COMMUNITY)
    html = render.render_issue(Issue(date=datetime(2026, 8, 15, tzinfo=UTC), items=[named]))
    assert "Jake Adams" in html


# ------------------------------------------------------------------- eval

def _seed(conn, *, slug, first_seen, published):
    """One row in the seen log, with control over both timestamps."""
    url = f"https://example.invalid/{slug}"
    conn.execute(
        "INSERT OR REPLACE INTO items (uid, source_id, source_name, title, url,"
        " canonical_url, published_at, author, raw_text, category_hint, first_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (slug, "src", "Some Outlet", slug, url, url, published, None, "body", None, first_seen),
    )


def test_eligible_excludes_items_seen_long_after_publication(tmp_path):
    """A YouTube channel hands back fifty videos on first fetch, most of them
    years old. Those never reached the scorer, and labelling them would measure
    nothing at the cost of an hour."""
    from dailydive import eval as eval_mod

    with store.connect(tmp_path / "db.sqlite3") as conn:
        _seed(conn, slug="fresh", first_seen="2026-08-15T00:00:00+00:00",
              published="2026-08-14T00:00:00+00:00")
        _seed(conn, slug="backcatalogue", first_seen="2026-08-15T00:00:00+00:00",
              published="2023-03-17T00:00:00+00:00")
        got = eval_mod.eligible(conn, max_age_days=7)

    assert [i.title for i in got] == ["fresh"]


def test_eval_reconstructs_the_uid_the_row_was_stored_under(tmp_path):
    """Labels are keyed by uid and so are scores, but eval rebuilds Items from
    the seen log rather than reading the stored uid — so the derivation has to
    round-trip. If it ever stops, labels quietly stop matching scores and the
    report is computed over an empty intersection, which looks like a clean run
    rather than a broken one."""
    from dailydive import eval as eval_mod

    original = item(title="A real headline", url="https://example.invalid/story?utm_source=x")
    with store.connect(tmp_path / "db.sqlite3") as conn:
        store.record_items(conn, [original])
        row = conn.execute("SELECT * FROM items").fetchone()
        assert eval_mod._item(row).uid == row["uid"] == original.uid


def test_prompt_hash_changes_when_the_prompt_does(tmp_path):
    """A hand-maintained version string is a thing somebody forgets to bump on
    the one edit that mattered."""
    from dailydive import score as score_module

    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("score things carefully", encoding="utf-8")
    b.write_text("score things carefully.", encoding="utf-8")

    assert score_module.prompt_hash(a) == score_module.prompt_hash(a)
    assert score_module.prompt_hash(a) != score_module.prompt_hash(b)
    assert len(score_module.prompt_hash(a)) == 12


def test_the_labelling_sheet_never_shows_the_model_score():
    """Anti-anchoring, and the reason it is a test rather than a note: a number
    in view decides the label before the reader has finished reading, so a sheet
    that leaks one produces an expensive confirmation of what the model already
    thought."""
    from dailydive import eval as eval_mod

    scored = item(
        title="How to dose alkalinity",
        extra={"relevance": "0.93", "gist": "A gist the model wrote", "beat": "MADEUPBEAT"},
        category_hint=Category.HUSBANDRY,
    )
    html = eval_mod.build_sheet([scored])

    assert "0.93" not in html
    assert "A gist the model wrote" not in html
    assert "MADEUPBEAT" not in html
    assert "How to dose alkalinity" in html   # the item itself is there


def test_labels_that_drifted_from_the_item_set_are_an_error(tmp_path):
    """A label file out of step with the items produces a number that looks
    fine and means nothing, which is worse than a crash."""
    from dailydive import eval as eval_mod

    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"labels": {"nosuchuid": "lead"}}), encoding="utf-8")

    with pytest.raises(ValueError, match="not in the item set"):
        eval_mod.load_labels(path, known={"realuid"})

    path.write_text(json.dumps({"labels": {"realuid": "brilliant"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown bucket"):
        eval_mod.load_labels(path, known={"realuid"})


class _Scored:
    def __init__(self, relevance, category=None):
        self.relevance = relevance
        self.category = category




def test_scores_round_trip_including_the_ones_below_threshold(tmp_path):
    """Keeping the drops is the point: they are the half that cannot be
    recovered later and the half that hides the expensive mistakes."""
    from dailydive.score import ItemScore

    scores = {
        "kept": ItemScore(uid="kept", category=Category.HUSBANDRY, relevance=0.9, gist="g"),
        "dropped": ItemScore(uid="dropped", category=None, relevance=0.1, gist="g"),
    }
    db = tmp_path / "db.sqlite3"
    with store.connect(db) as conn:
        assert store.record_scores(conn, scores, prompt_hash="abc123", model="m") == 2

    with store.connect(db) as conn:
        back = store.scores_for(conn, prompt_hash="abc123", model="m")
        assert set(back) == {"kept", "dropped"}
        assert back["dropped"].relevance == 0.1
        assert back["dropped"].category is None
        assert back["kept"].category is Category.HUSBANDRY
        # A different prompt version must not see the previous one's verdicts.
        assert store.scores_for(conn, prompt_hash="different", model="m") == {}


def _rep(pairs, threshold=0.45):
    """Build a report from (uid, label, relevance) triples."""
    from dailydive import eval as eval_mod

    items = {uid: item(title=uid) for uid, _, _ in pairs}
    labels = {uid: label for uid, label, _ in pairs}
    scores = {uid: _Scored(rel) for uid, _, rel in pairs}
    return eval_mod.report(labels, scores, items, threshold=threshold)


def test_a_buried_lead_is_an_error_but_a_buried_include_is_not():
    """The distinction the whole report rests on. The labels judge whether an
    item BELONGS; the threshold decides how many FIT. An include below the line
    may be correct rationing. A lead below the line cannot be — a lead is a
    story the editor would have put at the top of a section."""
    rep = _rep([
        ("buried_lead", "lead", 0.20),
        ("buried_include", "include", 0.20),
        ("kept_lead", "lead", 0.90),
    ])

    assert [r.uid for r in rep.leads_missed] == ["buried_lead"]
    assert [r.uid for r in rep.includes_below] == ["buried_include"]
    # The rationed include must not be counted as an error anywhere.
    assert "buried_include" not in {r.uid for r in rep.leads_missed + rep.drops_admitted}
    assert rep.by_source() == {"s": 1}


def test_a_drop_the_model_ran_is_an_unambiguous_error():
    rep = _rep([("shipped_junk", "drop", 0.70), ("dropped_junk", "drop", 0.10)])
    assert [r.uid for r in rep.drops_admitted] == ["shipped_junk"]


def test_borderline_items_are_listed_but_never_counted_as_errors():
    rep = _rep([("coinflip", "borderline", 0.90), ("other", "borderline", 0.10)])
    assert len(rep.borderline) == 2
    assert not rep.leads_missed and not rep.drops_admitted and not rep.includes_below


def test_precision_at_reports_what_it_measured_not_what_was_asked_for():
    """A run with fewer than n scored items must not count missing items as
    misses — that reads as a bad model rather than a short run."""
    rep = _rep([
        ("best", "lead", 0.95),
        ("good", "include", 0.80),
        ("junk", "drop", 0.60),
    ])
    assert rep.precision_at(2) == (2, 2)
    assert rep.precision_at(3) == (2, 3)
    assert rep.precision_at(20) == (2, 3)      # considered is 3, not 20


def test_precision_at_reads_the_models_ordering_not_the_input_order():
    rep = _rep([
        ("listed_first_but_scored_low", "drop", 0.10),
        ("listed_last_but_scored_high", "lead", 0.99),
    ])
    assert [r.uid for r in rep.ranked][0] == "listed_last_but_scored_high"
    assert rep.precision_at(1) == (1, 1)


def test_spearman_handles_the_perfect_reversed_and_tied_cases():
    """Ties are the normal case here, not an edge case: the editor's scale has
    four values across 128 items, so almost everything is tied."""
    from dailydive import eval as eval_mod

    assert eval_mod.spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert eval_mod.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # Heavy ties on one side, perfectly ordered otherwise. rho is strong but
    # capped below 1.0 at 4/sqrt(20), because tied ranks cannot express an
    # ordering as fine as the other series has. That ceiling matters when
    # reading the real report: with four buckets over 128 items the editor's
    # series is mostly ties, so a middling rho may be the scale's limit rather
    # than the model's failing.
    assert eval_mod.spearman([0.1, 0.2, 0.8, 0.9], [0, 0, 3, 3]) == pytest.approx(0.894427, abs=1e-5)
    # A constant series has no measurable correlation. None, not 0.0, which
    # would read as "no relationship" rather than "not measurable".
    assert eval_mod.spearman([0.5, 0.5, 0.5], [1, 2, 3]) is None
    assert eval_mod.spearman([1.0], [1.0]) is None


def test_a_labelled_item_that_was_never_scored_is_reported_not_assumed():
    """Treating a missing score as a drop would silently invent agreement."""
    from dailydive import eval as eval_mod

    items = {"orphan": item(title="Never scored")}
    rep = eval_mod.report({"orphan": "lead"}, {}, items, threshold=0.45)

    assert rep.unscored == ["orphan"]
    assert rep.scored == 0
    assert not rep.leads_missed


def test_a_report_with_nothing_scored_says_so_instead_of_printing_a_score():
    """The failure I'd most likely ship: a confident 0% built from no data."""
    from dailydive import eval as eval_mod

    items = {"orphan": item(title="Never scored")}
    rep = eval_mod.report({"orphan": "lead"}, {}, items, threshold=0.45)
    text = eval_mod.format_report(rep)

    assert "Nothing to measure" in text
    assert "0%" not in text


def test_the_committed_labels_still_match_the_items_they_describe():
    """Labels are data, not output. If the seen log or the uid derivation moves
    under them, they silently stop matching and every future report is computed
    over an empty intersection — which looks like a clean run."""
    from dailydive import eval as eval_mod

    path = Path("tests/fixtures/labels.json")
    labels = eval_mod.load_labels(path)
    assert len(labels) == 128
    assert set(labels.values()) <= set(eval_mod.BUCKETS)


def test_stored_and_fresh_scores_are_interchangeable(tmp_path):
    """The eval merges scores read from the DB with freshly-scored ones into one
    dict, so both have to answer to the same interface. They did not: scores_for
    returned sqlite3.Row, which is subscript-only, and report() reads
    .relevance. It surfaced as an AttributeError the first time the report ran
    end to end, which is late.

    The category is rebuilt as an enum for the same reason — anything reading
    it should not have to know which source the score came from."""
    from dailydive import eval as eval_mod
    from dailydive.score import ItemScore

    fresh = ItemScore(uid="u", category=Category.HUSBANDRY, relevance=0.7, gist="g")
    with store.connect(tmp_path / "db.sqlite3") as conn:
        store.record_scores(conn, {"u": fresh}, prompt_hash="p", model="m")
        stored = store.scores_for(conn, prompt_hash="p", model="m")["u"]

    assert stored.relevance == fresh.relevance
    assert stored.category == fresh.category
    assert isinstance(stored.category, Category)

    # Both shapes must survive the same code path and rank identically.
    items = {"u": item(title="A story", category_hint=Category.HUSBANDRY)}
    for score in (fresh, stored):
        rep = eval_mod.report({"u": "include"}, {"u": score}, items, threshold=0.45)
        assert rep.scored == 1
        assert rep.ranked[0].relevance == 0.7


# ---------------------------------------------------------------- delivery

def _email(**kw) -> str:
    from dailydive import render

    issue = Issue(
        date=datetime(2026, 8, 21, tzinfo=UTC),
        items=kw.pop("items", [item(category_hint=Category.COMMUNITY)]),
    )
    return render.render_email(issue, **kw)


def test_the_email_avoids_everything_mail_clients_break_on():
    """issue.html.j2 cannot be reused and this is why. Custom properties and
    color-mix() are unsupported in every major client; sticky, grid and flex
    are ignored by Outlook; a <style> block can be stripped outright. A digest
    that arrives unstyled is a digest nobody reads."""
    html = _email()
    for banned in ("color-mix(", "var(--", "position:sticky", "position: sticky",
                   "display:flex", "display: grid", "<style"):
        assert banned not in html, banned


def test_every_item_in_the_email_is_credited_with_an_absolute_url():
    """Attribution applies in an inbox exactly as on the page — and a relative
    href, which merely looks wrong on the web, is meaningless in email."""
    from dailydive import render

    it = item(source_name="Reef Builders", url="https://reefbuilders.com/story",
              category_hint=Category.INDUSTRY)
    html = render.render_email(Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[it]))

    assert "Reef Builders" in html
    assert 'href="https://reefbuilders.com/story"' in html
    # No root-relative links anywhere: they resolve against nothing in a mail client.
    assert 'href="/' not in html
    assert 'src="/' not in html


def test_the_subject_names_the_lead_story():
    """"Weekly Dive — August 21" tells a reader only what they subscribed to."""
    from dailydive import render

    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC),
                  items=[item(title="Fluval unveils a new gyre pump")])
    assert render.subject(issue) == f"{brand.PUBLICATION} — Fluval unveils a new gyre pump"

    # An issue with no items still needs a subject rather than a dangling dash.
    empty = Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[])
    assert render.subject(empty).startswith(f"{brand.PUBLICATION} — ")
    assert "Friday, August 21, 2026" in render.subject(empty)


def test_sending_an_empty_issue_is_refused():
    """An email whose body is a masthead and a footer spends subscriber
    goodwill to say nothing, and goodwill is all a newsletter has."""
    from dailydive import deliver

    with pytest.raises(deliver.DeliveryError, match="no items"):
        deliver.send(Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[]), "<p>x</p>")


def test_a_refused_send_raises_rather_than_logging_and_moving_on(monkeypatch):
    """A send that fails quietly is a week nobody receives, noticed by nobody.
    The run has to go red."""
    from dailydive import deliver

    monkeypatch.setenv(deliver.ENV_KEY, "k")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="that field is not valid")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item()])
    with pytest.raises(deliver.DeliveryError, match="422"):
        deliver.send(issue, "<p>x</p>", client=client)


def test_a_successful_send_reports_what_it_did(monkeypatch):
    from dailydive import deliver

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get(deliver.AUTH_HEADER)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "abc"})

    monkeypatch.setenv(deliver.ENV_KEY, "secret-key")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC),
                  items=[item(title="A headline")])

    out = deliver.send(issue, "<p>body</p>", client=client)
    assert "sent" in out
    assert seen["url"] == deliver.EMAILS_ENDPOINT
    assert seen["auth"] == "Token secret-key"
    assert seen["body"][deliver.FIELD_SUBJECT] == f"{brand.PUBLICATION} — A headline"
    assert seen["body"][deliver.FIELD_STATUS] == deliver.STATUS_SEND


def test_the_api_key_comes_from_the_environment_and_nowhere_else(monkeypatch):
    """The repo is public. A committed key is a leaked credential, not a
    configuration mistake."""
    from dailydive import deliver

    monkeypatch.delenv(deliver.ENV_KEY, raising=False)
    with pytest.raises(deliver.DeliveryError, match="not set"):
        deliver.api_key()

    monkeypatch.setenv(deliver.ENV_KEY, "   ")
    with pytest.raises(deliver.DeliveryError, match="not set"):
        deliver.api_key()


def test_the_dry_run_redacts_the_key_and_sends_nothing(monkeypatch):
    """It exists to be pasted next to the docs, which means it must be safe to
    paste anywhere."""
    from dailydive import deliver

    monkeypatch.setenv(deliver.ENV_KEY, "super-secret")
    issue = Issue(date=datetime(2026, 8, 21, tzinfo=UTC), items=[item()])
    out = deliver.preview(issue, "<p>" + "x" * 5000 + "</p>")

    assert "super-secret" not in out
    assert "****" in out
    assert deliver.EMAILS_ENDPOINT in out
    # The body is summarised, not dumped — 40KB of table markup defeats the point.
    assert "x" * 100 not in out


def test_the_subscribe_page_points_at_the_list_and_needs_no_javascript(tmp_path):
    """A redirect that needs a script fails silently in exactly the readers most
    likely to block one."""
    from dailydive import deliver, render

    html = render.write_subscribe(tmp_path).read_text(encoding="utf-8")
    assert deliver.SUBSCRIBE_URL in html
    assert "http-equiv=\"refresh\"" in html
    assert "<script" not in html
    # A real link too, for anything that ignores the meta refresh.
    assert f'href="{deliver.SUBSCRIBE_URL}"' in html
