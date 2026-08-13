# The Daily Dive

A Techmeme-style morning digest for the saltwater reef hobby, published at
[theloneaquarist.com](https://www.theloneaquarist.com).

Reads the feeds that matter — publications, forums, YouTube channels, and NOAA's
reef data — dedupes what overlaps, sorts it into categories, and produces one
scannable page every morning. Every item links to its source.

**Status: v0.** The pipeline works end to end with no AI in it: fetch, normalize,
dedupe, render. Headlines are the sources' own. The model passes come in v1.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

.venv/bin/daily-dive sources               # what's configured
.venv/bin/daily-dive run --offline         # build from fixtures, no network
.venv/bin/daily-dive run                   # build from live feeds
.venv/bin/pytest -q                        # 18 tests, no network, no cost
```

Output lands in `site/` — `index.html` plus a dated permalink under `site/issues/`.

Useful while iterating:

```bash
daily-dive run --source reefbuilders --limit 5    # one feed, small
daily-dive run --offline --out site/preview       # scratch output, gitignored
```

## Adding sources

Edit [`sources.toml`](sources.toml). One `[[source]]` block per feed.

**The rule: RSS/API only.** If a source has no feed, it doesn't go in. No HTML
scraping, no headless browser. That keeps this cheap to run, hard to break, and
welcome to the outlets it links to.

Three YouTube channels are configured but parked with `enabled = false`, because
per-channel RSS needs the `UC...` channel ID and there's no handle-based feed
endpoint. To enable one: open the channel, View Source, search for `channelId`,
paste it into the URL, flip `enabled = true`. A wrong ID returns an empty feed
rather than an error, so verify with `--source <id>` before trusting it.

## Attribution

The whole project depends on being a good citizen about this, so the rules are
enforced in code rather than left to convention:

- `Item` cannot be constructed without `source_name`, `title`, a resolvable
  `url`, and `published_at`. Feed entries missing any of these are dropped with
  a warning, never published half-cited.
- `render.assert_attributable` re-checks at the publish boundary, so a bug
  upstream raises at build time instead of shipping an uncredited line.
- Every issue footer names every outlet that appears.
- The fetcher honors `robots.txt`, identifies itself with a contact address,
  limits itself to one request per second per host, and sends conditional-GET
  headers so a normal morning re-fetches almost nothing.
- Summaries, once v2 adds them, are capped at 40 words with no verbatim quote
  over ~25 words. Facts and links, not reproduction.

If you run something that appears here and want it removed, the address in
`dailydive/ingest.py` reaches a human.

## The industry beat

[`docs/industry-brief.md`](docs/industry-brief.md) is the editorial authority for
industry coverage: the ownership map, verification standards, sourcing hierarchy,
and — most importantly — the language rules that keep a distribution deal from
being reported as an acquisition.

[`industry.toml`](industry.toml) is the machine-readable half, and
`dailydive/entities.py` resolves brands to their owners against it with **no model
calls**. That's deliberate: a deterministic lookup can't hallucinate an ownership
relationship, so "Radion" reliably resolves to EcoTech Marine → Aperture Pet &
Life → Bertram Capital, and never to anything that isn't written in the file.

Two judgement calls worth knowing about:

- **Ambiguous aliases are deliberately not matched.** `AI` is a real
  AquaIllumination alias, and `Apex`, `Prime`, `Gyre`, `Dart`, and `Speedy` are
  all real product names — matching them automatically would tag every artificial
  intelligence story as aquarium lighting news. They're recorded in
  `ambiguous_aliases` for a human reader and skipped by the matcher.
- **Distributors never appear as parents.** CoralVue distributes Abyzz; Aperture
  once distributed Maxspect. Neither is ownership, and there's a test asserting
  the map never says otherwise.

What still needs the model passes: judging whether a story is *material*,
assigning the beat (Ownership / Leadership / Distribution / Product / Safety /
Manufacturing / Financial), writing the "why it matters" line, and applying the
brief's two-source rule for acquisitions and recalls. Those arrive with v1 and v2.

## Layout

```
sources.toml          the file you'll edit most
dailydive/
  models.py           Item / Source / Issue — attribution invariants live here
  config.py           sources.toml -> Source
  ingest.py           robots.txt, rate limiting, conditional GET
  normalize.py        feed dialects in, one Item model out
  store.py            SQLite: archive + HTTP cache
  render.py           Issue -> HTML
  cli.py              entry point
templates/            Jinja
tests/fixtures/       synthetic feeds; also the eval set for later prompt work
site/                 generated output, served by GitHub Pages
```

`dailydive.sqlite3` is committed on purpose: it's the dedupe memory and the
archive, it's free, and it versions both for free.

## Roadmap

| | |
|---|---|
| **v0** ✅ | Pipeline with no AI. Fetch, dedupe, render, full attribution. |
| **v1** | Haiku pass scores and categorizes every item. Sections get real. |
| **v2** | Clustering + Sonnet writes the issue + a grounding check. The Techmeme format proper. |
| **v3** | RSS out, then email from `mail.theloneaquarist.com`. |

Ship each one fully before starting the next. The temptation is to start at v2.

## Cost

Target is a few dollars a month: GitHub Actions and Pages are free on a public
repo, and the model spend is ~150 short classification calls plus one long
writing call per day. v0 spends nothing at all — there are no model calls in it.

## Note on sandboxed environments

Cloud sessions with `Trusted` network access can reach package registries but
not arbitrary sites, so live feed fetching returns 403 at the proxy. Use
`--offline` there, and run live fetches locally or from an environment with
wider network access.
