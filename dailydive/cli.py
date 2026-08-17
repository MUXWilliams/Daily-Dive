"""Command line entry point.

    daily-dive run                      fetch everything, write site/
    daily-dive run --source reefbuilders --limit 5
    daily-dive run --offline            build from tests/fixtures, no network
    daily-dive sources                  list configured feeds
"""

from __future__ import annotations

import argparse
import imaplib
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import archive, brand, config, ingest, mailbox, normalize, picks, render, thumbs, youtube
from . import score as score_mod
from . import store
from .models import Issue, Item, Source, SourceType
from .pricing import RunSpend

log = logging.getLogger("dailydive")

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _collect_mail(source: Source, days: int, client: httpx.Client) -> list[Item]:
    """Newsletters for one IMAP source, or nothing if we have no credentials."""
    user = os.environ.get(ingest.IMAP_USER_ENV, "").strip() or brand.CONTACT_EMAIL
    password = os.environ.get(ingest.IMAP_PASSWORD_ENV, "").strip()
    if not password:
        log.warning("skipping %s: no %s in the environment", source.id, ingest.IMAP_PASSWORD_ENV)
        return []
    try:
        return mailbox.fetch(source, user=user, password=password, days=days, client=client)
    except (imaplib.IMAP4.error, OSError) as exc:
        # A mailbox that won't open should cost the newsletter section, not
        # the issue — same posture as a feed that 500s.
        log.error("failed %s: %s", source.id, exc)
        return []


def _collect_live(sources: list[Source], db: Path, days: int) -> tuple[list[Item], list[Item]]:
    """Fetch everything, and say which of it is new.

    Returns (everything fetched, items not seen in a previous run). The first
    is what the volume table measures — "is this outlet publishing?" is a
    question about the feed, not about our archive. The second is what an
    issue may contain: a story that ran last week is not news this week, even
    while it sits inside the recency window.
    """
    items: list[Item] = []
    with store.connect(db) as conn, ingest.Fetcher() as fetcher:
        for source in sources:
            if source.type is SourceType.IMAP:
                found = _collect_mail(source, days, fetcher._client)
                log.info("%s: %d items", source.id, len(found))
                items.extend(found)
                continue
            try:
                result = fetcher.fetch(source, conn)
            except ingest.RobotsDisallowed as exc:
                log.warning("skipping %s: %s", source.id, exc)
                continue
            except ingest.MissingCredential as exc:
                # Not fatal: a missing key should cost you the video section,
                # not the issue.
                log.warning("skipping %s: %s", source.id, exc)
                continue
            except httpx.HTTPError as exc:
                log.error("failed %s: %s", source.id, exc)
                continue

            if result.body is None:  # 304, nothing changed
                continue

            found = normalize.normalize(source, result.body)
            log.info("%s: %d items", source.id, len(found))
            items.extend(found)

        items = normalize.dedupe(items)
        # Ask before recording: once record_items runs, everything is known.
        seen = store.known_uids(conn, [i.uid for i in items])
        fresh = [i for i in items if i.uid not in seen]
        store.record_items(conn, items)
        log.info("%d items (%d new to the archive)", len(items), len(fresh))
    return items, fresh


def _drop_shorts(items: list[Item]) -> list[Item]:
    """Filter Shorts out, if there are any videos and we have a key."""
    if not any(i.extra.get("video_id") for i in items):
        return items

    key = os.environ.get(ingest.API_KEY_ENV[SourceType.YOUTUBE_API], "").strip()
    if not key:
        log.warning("no YouTube key — cannot check durations, so Shorts may appear")
        return items

    with httpx.Client(timeout=ingest.TIMEOUT, headers={"User-Agent": ingest.USER_AGENT}) as client:
        return youtube.drop_shorts(items, client=client, api_key=key)


def _collect_offline(sources: list[Source]) -> list[Item]:
    """Build from committed fixtures. No network, no cost, fast loop."""
    items: list[Item] = []
    for source in sources:
        fixture = FIXTURE_DIR / f"{source.id}.xml"
        if not fixture.exists():
            log.warning("no fixture for %s at %s", source.id, fixture)
            continue
        found = normalize.normalize(source, fixture.read_bytes())
        log.info("%s: %d items (fixture)", source.id, len(found))
        items.extend(found)
    return normalize.dedupe(items)


# Set by the Actions runner. Absent locally, which is why a missing token is a
# skipped section and not an error — same posture as a missing YouTube key.
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
GITHUB_REPO_ENV = "GITHUB_REPOSITORY"


def _bucket() -> picks.Bucket | None:
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    repo = os.environ.get(GITHUB_REPO_ENV, "").strip()
    if not token or not repo:
        log.info("no %s/%s — skipping the pick bucket", GITHUB_TOKEN_ENV, GITHUB_REPO_ENV)
        return None
    return picks.Bucket(repo, token)


def _collect_picks(db: Path) -> tuple[list[Item], list[tuple[int, str]]]:
    """Read the bucket, rejecting what cannot run and saying why.

    A bucket that cannot be reached costs the picks, not the issue — the same
    rule every other source follows.
    """
    bucket = _bucket()
    if bucket is None:
        return [], []
    try:
        with store.connect(db) as conn:
            already = store.published_uids(conn)
        items, rejected = picks.collect(bucket, published_uids=already)
    except httpx.HTTPError as exc:
        log.error("could not read the pick bucket: %s", exc)
        return [], []

    for number, reason in rejected:
        log.warning("pick #%d rejected: %s", number, reason)
        try:
            bucket.comment(number, f"{reason}\n\nLeaving this open so it can be fixed.")
        except httpx.HTTPError as exc:
            log.error("could not comment on pick #%d: %s", number, exc)

    log.info("%d pick(s) accepted, %d rejected", len(items), len(rejected))
    return items, rejected


def _answer_picks(issue: Issue) -> None:
    """Close the issues whose picks made the page, saying where they landed.

    Only the ones that survived to the end: a pick merged away by
    collapse_similar did not run, and telling the editor it did would be a lie
    they would find out about on Friday.
    """
    bucket = _bucket()
    if bucket is None:
        return
    permalink = f"{brand.SITE_URL}/issues/{issue.date:%Y-%m-%d}.html"
    for item in issue.items:
        number = item.extra.get("pick_issue")
        if not picks.is_pick(item) or not number:
            continue
        try:
            bucket.close(
                int(number),
                f"Published in the {render._datefmt(issue.date)} issue "
                f"under **{item.category_hint}**.\n\n{permalink}",
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.error("could not close pick #%s: %s", number, exc)


def _is_publishing_run(args: argparse.Namespace) -> bool:
    """Whether this run's output is going to actually reach readers.

    Mirrors the publish gate in .github/workflows/daily.yml on purpose. The
    workflow decides whether to deploy; this decides whether to record the
    consequences of deploying. Both have to agree, or the archive says a story
    ran when no page carrying it was ever served.
    """
    return not args.offline and not args.source and not args.limit


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument surface, built separately from running it.

    Split out so a test can check what the parser accepts without executing a
    run — specifically, that every flag the GitHub workflow passes is one this
    parser knows about. That mismatch has broken CI before, and it fails in the
    worst possible place: after the install and test steps have already passed.
    """
    # Shared options live on a parent parser so they're accepted on either side
    # of the subcommand — `daily-dive -v run` and `daily-dive run -v` both work.
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS matters: the parent and the subparser share this action, so with
    # an ordinary `default=False` the subparser writes its default over a -v
    # given before the subcommand, and `daily-dive -v run` silently isn't
    # verbose. Suppressed, the flag is only ever set by actually passing it —
    # which is why main reads it with getattr.
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(prog="daily-dive", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="build an issue", parents=[common])
    run.add_argument("--source", action="append", help="only these source ids (repeatable)")
    run.add_argument("--limit", type=int, help="cap items per run, newest first")
    run.add_argument("--offline", action="store_true", help="use tests/fixtures instead of the network")
    run.add_argument(
        "--max-age-days",
        type=int,
        default=normalize.DEFAULT_MAX_AGE_DAYS,
        help=f"drop items older than this (default {normalize.DEFAULT_MAX_AGE_DAYS}; 0 keeps everything)",
    )
    run.add_argument("--out", type=Path, default=Path("site"), help="output directory (default: site)")
    run.add_argument("--db", type=Path, default=store.DEFAULT_DB)
    run.add_argument("--sources-file", type=Path, default=config.DEFAULT_SOURCES)
    run.add_argument(
        "--score",
        action="store_true",
        help="run the Haiku scoring pass (needs ANTHROPIC_API_KEY; costs money)",
    )
    run.add_argument(
        "--no-picks",
        action="store_true",
        help="skip the editor's pick bucket (needs GITHUB_TOKEN otherwise)",
    )
    run.add_argument(
        "--keep-shorts",
        action="store_true",
        help="don't filter out YouTube Shorts (they are dropped by default)",
    )
    run.add_argument(
        "--print",
        dest="print_issue",
        action="store_true",
        help="also print the issue as plain text (readable in a CI log)",
    )
    run.add_argument(
        "--threshold",
        type=float,
        default=None,
        help=f"minimum relevance to publish (default {score_mod.DEFAULT_THRESHOLD})",
    )

    checking = sub.add_parser(
        "mailcheck",
        help="verify the newsletter mailbox and list its senders (publishes nothing)",
        parents=[common],
    )
    checking.add_argument("--source", default="mail-aquaticmedia", help="which imap source to check")
    checking.add_argument("--days", type=int, default=30, help="how far back to look")
    checking.add_argument("--sources-file", type=Path, default=config.DEFAULT_SOURCES)

    previewing = sub.add_parser(
        "preview",
        help="render the template against a frozen issue (no network, no cost)",
        parents=[common],
    )
    previewing.add_argument(
        "--out", type=Path, default=Path("site/preview"), help="output directory"
    )
    previewing.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help="also write an artifact-shaped file (no doctype/head/body wrapper)",
    )
    previewing.add_argument(
        "--linked-assets",
        action="store_true",
        help="reference assets/ instead of inlining them (smaller file, only opens in place)",
    )

    listing = sub.add_parser("sources", help="list configured feeds", parents=[common])
    listing.add_argument("--sources-file", type=Path, default=config.DEFAULT_SOURCES)

    probing = sub.add_parser(
        "probe",
        help="test candidate feed URLs (needs real network access)",
        parents=[common],
    )
    probing.add_argument("urls", nargs="*", help="URLs to test; omit to use the built-in candidate list")
    probing.add_argument("--markdown", action="store_true", help="emit a markdown table")
    probing.add_argument(
        "--discover",
        action="store_true",
        help="also ask known sites what feeds they advertise (HTML autodiscovery)",
    )

    evaluating = sub.add_parser(
        "eval",
        help="measure the scorer against hand-labelled items",
        parents=[common],
    )
    eval_sub = evaluating.add_subparsers(dest="eval_command", required=True)

    sheet = eval_sub.add_parser("sheet", help="build the labelling page", parents=[common])
    sheet.add_argument("--out", type=Path, required=True, help="where to write the HTML")
    sheet.add_argument("--db", type=Path, default=store.DEFAULT_DB)
    sheet.add_argument(
        "--max-age-days",
        type=int,
        default=normalize.DEFAULT_MAX_AGE_DAYS,
        help="how fresh an item had to be when first seen to count as scored",
    )
    sheet.add_argument("--limit", type=int, default=None, help="cap the number of items")

    rep = eval_sub.add_parser(
        "report", help="score the labelled items and compare", parents=[common]
    )
    rep.add_argument("--labels", type=Path, required=True, help="the exported label JSON")
    rep.add_argument("--db", type=Path, default=store.DEFAULT_DB)
    rep.add_argument(
        "--max-age-days", type=int, default=normalize.DEFAULT_MAX_AGE_DAYS
    )
    rep.add_argument(
        "--threshold", type=float, default=score_mod.DEFAULT_THRESHOLD,
        help=f"the shipping threshold to judge against (default {score_mod.DEFAULT_THRESHOLD})",
    )
    rep.add_argument(
        "--rescore",
        action="store_true",
        help="call the API for items with no stored score under the current prompt",
    )

    return parser


def _cmd_eval(args) -> int:
    from . import eval as eval_mod

    with store.connect(args.db) as conn:
        items = eval_mod.eligible(conn, max_age_days=args.max_age_days)

    if args.eval_command == "sheet":
        if args.limit:
            items = items[: args.limit]
        if not items:
            log.error("no eligible items — nothing to label")
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(eval_mod.build_sheet(items), encoding="utf-8")
        print(f"wrote {args.out} ({len(items)} items to label)")
        return 0

    by_uid = {i.uid: i for i in items}
    labels = eval_mod.load_labels(args.labels, known=set(by_uid))

    prompt_hash = score_mod.prompt_hash()
    with store.connect(args.db) as conn:
        stored = store.scores_for(conn, prompt_hash=prompt_hash, model=score_mod.MODEL)

    missing = [by_uid[uid] for uid in labels if uid not in stored]
    if missing and args.rescore:
        try:
            import anthropic
        except ImportError:
            log.error("--rescore needs the anthropic package: pip install -e '.[ai]'")
            return 2
        log.info("scoring %d labelled item(s) not yet seen under prompt %s", len(missing), prompt_hash)
        spend = RunSpend()
        fresh = score_mod.score_items(
            missing, client=anthropic.Anthropic(), spend=spend.stage("eval", score_mod.MODEL)
        )
        with store.connect(args.db) as conn:
            store.record_scores(conn, fresh, prompt_hash=prompt_hash, model=score_mod.MODEL)
        stored.update(fresh)
        print("cost:\n" + spend.report())
    elif missing:
        log.warning(
            "%d labelled item(s) have no score under prompt %s — pass --rescore to fill them",
            len(missing),
            prompt_hash,
        )

    result = eval_mod.report(labels, stored, by_uid, threshold=args.threshold)
    print(f"prompt {prompt_hash} · model {score_mod.MODEL}\n")
    print(eval_mod.format_report(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "eval":
        return _cmd_eval(args)

    if args.command == "probe":
        from . import probe as probe_mod

        results = probe_mod.probe(args.urls or None, discover=args.discover)
        if args.markdown:
            print(probe_mod.format_markdown(results))
        else:
            for r in results:
                print(f"{r.icon} {r.verdict:14} {r.entries or '':>4}  {r.url}\n     {r.detail}")
        return 0 if any(r.verdict == "ok" for r in results) else 1

    if args.command == "mailcheck":
        wanted = [
            s for s in config.load_sources(args.sources_file, include_disabled=True)
            if s.id == args.source
        ]
        if not wanted:
            log.error("no source with id %r", args.source)
            return 2
        password = os.environ.get(ingest.IMAP_PASSWORD_ENV, "").strip()
        if not password:
            log.error("%s is not set", ingest.IMAP_PASSWORD_ENV)
            return 2
        user = os.environ.get(ingest.IMAP_USER_ENV, "").strip() or brand.CONTACT_EMAIL
        try:
            print(mailbox.describe(wanted[0], user=user, password=password, days=args.days))
        except (imaplib.IMAP4.error, OSError) as exc:
            log.error("mailbox check failed: %s", exc)
            return 1
        return 0

    if args.command == "preview":
        from . import preview as preview_mod

        path = preview_mod.write_preview(args.out, standalone=not args.linked_assets)
        print(f"wrote {path}")
        if args.artifact:
            args.artifact.parent.mkdir(parents=True, exist_ok=True)
            args.artifact.write_text(preview_mod.artifact_html(args.out), encoding="utf-8")
            print(f"wrote {args.artifact}")
        return 0

    if args.command == "sources":
        for source in config.load_sources(args.sources_file, include_disabled=True):
            mark = " " if source.enabled else "×"
            print(f"{mark} {source.id:22} {source.type.value:10} {source.display_name}")
        return 0

    sources = config.load_sources(args.sources_file)
    if args.source:
        wanted = set(args.source)
        sources = [s for s in sources if s.id in wanted]
        missing = wanted - {s.id for s in sources}
        if missing:
            log.error("unknown or disabled source ids: %s", ", ".join(sorted(missing)))
            return 2

    if not sources:
        log.error("no enabled sources — check %s", args.sources_file)
        return 2

    if args.offline:
        fetched = _collect_offline(sources)
        items = fetched
    else:
        fetched, items = _collect_live(sources, args.db, args.max_age_days)
        if len(items) < len(fetched):
            log.info("%d item(s) already ran in an earlier issue", len(fetched) - len(items))
    if fetched:
        # Volume measures the feeds, so it counts everything fetched — an
        # outlet that published twice this week published twice, whether or
        # not we already carried those stories.
        print("volume:\n" + normalize.volume_report(fetched))
    if not args.offline and not args.keep_shorts:
        items = _drop_shorts(items)
    # Before scoring, not after: an item too old to publish shouldn't be paid
    # for. Scoring is the one step that costs money.
    items = normalize.recent(items, days=args.max_age_days)
    if args.limit:
        items = items[: args.limit]

    spend = RunSpend()
    if args.score:
        try:
            import anthropic
        except ImportError:
            log.error("--score needs the anthropic package: pip install -e '.[ai]'")
            return 2

        before = len(items)
        client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / ant profile
        stage = spend.stage("score", score_mod.MODEL)
        scores = score_mod.score_items(items, client=client, spend=stage)

        # Recorded before the threshold is applied, so the drops survive. An
        # item the editor would have run and the model discarded leaves no
        # trace anywhere else — no page, no log line, nothing to notice — so
        # this table is the only place that class of error can be found.
        # Unconditional, unlike `published`: a partial run's verdicts are just
        # as real as a full run's, and claim nothing about publication.
        with store.connect(args.db) as conn:
            written = store.record_scores(
                conn, scores, prompt_hash=score_mod.prompt_hash(), model=score_mod.MODEL
            )
        log.info("recorded %d score(s) for prompt %s", written, score_mod.prompt_hash())

        items = score_mod.apply_scores(
            items,
            scores,
            threshold=args.threshold if args.threshold is not None else score_mod.DEFAULT_THRESHOLD,
            community_sources=frozenset(s.id for s in sources if s.is_community),
        )
        log.info("scored %d items, kept %d", before, len(items))
        print("cost:\n" + spend.report())

    # Picks join here: after scoring, so the model can never drop a story the
    # editor deliberately chose, and before collapse_similar, so a pick and the
    # crawler's coverage of the same story merge instead of both running.
    #
    # First in the list is load-bearing twice over. collapse_similar keeps the
    # first of each group, so the pick survives and the crawled version becomes
    # its "+N similar" credit; and the renderer preserves list order inside a
    # section, so a pick leads its section. Both are what "a pick outranks the
    # model" means in practice.
    bucket_items: list[Item] = []
    if not args.offline and not args.no_picks:
        bucket_items, rejected = _collect_picks(args.db)
        items = bucket_items + items
        if args.score or bucket_items:
            deduped = normalize.collapse_similar(items)
            if len(deduped) < len(items):
                log.info("collapsed %d near-duplicate item(s) after picks", len(items) - len(deduped))
            items = deduped

    issue = Issue(date=datetime.now(UTC), items=items)

    # The Resource video's thumbnail, fetched before the render so write_issue
    # finds it on disk. Only on a publishing run: a --source or --limit build is
    # a probe, and a probe should not be writing assets into the site or
    # committing binaries nobody asked for. Failure is logged inside and costs
    # the picture, never the issue.
    resource = render.pick_resource(issue)
    if resource is not None and _is_publishing_run(args):
        thumbs.fetch(resource.extra["video_id"], args.out)

    path = render.write_issue(issue, args.out)
    # Unconditional, unlike the archive below: the about page says nothing about
    # this issue, so a partial run rewriting it claims nothing it shouldn't. It
    # is also the page most likely to be stale, having been static until now.
    render.write_about(args.out)

    # Everything below claims the issue reached readers, and only a full run
    # does. The workflow refuses to deploy a --source or --limit build because
    # it is knowingly partial; without the same test here, a five-item test run
    # would mark those items published and close the pick issues that fed it —
    # with a link to a page nobody deployed.
    if _is_publishing_run(args):
        with store.connect(args.db) as conn:
            fresh = store.record_published(conn, issue.items, issue.date)
        log.info("recorded %d newly published item(s)", fresh)
        entries = archive.record(args.out, issue)
        archive.write_page(args.out)
        log.info("archive now lists %d issue(s)", len(entries))
        if bucket_items:
            _answer_picks(issue)
    elif bucket_items:
        log.info(
            "%d pick(s) included but left open: a partial run doesn't publish",
            len(bucket_items),
        )
    print(f"wrote {path} ({len(items)} items from {len(issue.outlets)} outlets)")
    if args.print_issue:
        print()
        print(render.as_text(issue))
    return 0


if __name__ == "__main__":
    sys.exit(main())
