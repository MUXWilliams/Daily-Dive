"""Command line entry point.

    daily-dive run                      fetch everything, write site/
    daily-dive run --source reefbuilders --limit 5
    daily-dive run --offline            build from tests/fixtures, no network
    daily-dive sources                  list configured feeds
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import config, ingest, normalize, render
from . import score as score_mod
from . import store
from .models import Issue, Item, Source
from .pricing import RunSpend

log = logging.getLogger("dailydive")

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _collect_live(sources: list[Source], db: Path) -> list[Item]:
    items: list[Item] = []
    with store.connect(db) as conn, ingest.Fetcher() as fetcher:
        for source in sources:
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
        new_count = store.record_items(conn, items)
        log.info("%d items (%d new to the archive)", len(items), new_count)
    return items


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "probe":
        from . import probe as probe_mod

        results = probe_mod.probe(args.urls or None, discover=args.discover)
        if args.markdown:
            print(probe_mod.format_markdown(results))
        else:
            for r in results:
                print(f"{r.icon} {r.verdict:14} {r.entries or '':>4}  {r.url}\n     {r.detail}")
        return 0 if any(r.verdict == "ok" for r in results) else 1

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

    items = _collect_offline(sources) if args.offline else _collect_live(sources, args.db)
    if items:
        print("volume:\n" + normalize.volume_report(items))
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
        items = score_mod.apply_scores(
            items,
            scores,
            threshold=args.threshold if args.threshold is not None else score_mod.DEFAULT_THRESHOLD,
        )
        log.info("scored %d items, kept %d", before, len(items))
        print("cost:\n" + spend.report())

    issue = Issue(date=datetime.now(UTC), items=items)
    path = render.write_issue(issue, args.out)
    print(f"wrote {path} ({len(items)} items from {len(issue.outlets)} outlets)")
    if args.print_issue:
        print()
        print(render.as_text(issue))
    return 0


if __name__ == "__main__":
    sys.exit(main())
