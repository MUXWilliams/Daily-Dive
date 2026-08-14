"""v0 pipeline tests. No network, no model calls, no cost."""

from __future__ import annotations

import json
import re

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from dailydive import cli, config, ingest, normalize, render, store
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
    video = item(
        url="https://www.youtube.com/watch?v=abc123XYZ_1",
        category_hint=Category.HUSBANDRY,
        extra={"video_id": "abc123XYZ_1", "duration_s": "1102"},
    )
    html = render.render_issue(Issue(date=datetime(2026, 8, 14, tzinfo=UTC), items=[video]))
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
    prompt = Path("prompts/score.system.md").read_text(encoding="utf-8")
    assert "tangential" in prompt
    assert "0.2 or below" in prompt


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
