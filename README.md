# The Daily Dive

A Techmeme-style morning digest for the saltwater reef hobby, published at
[theloneaquarist.com](https://www.theloneaquarist.com).

Reads the feeds that matter — publications, forums, YouTube channels, and NOAA's
reef data — dedupes what overlaps, sorts it into categories, and produces one
scannable page every morning. Every item links to its source.

**Status: v1.** The pipeline fetches, normalizes, dedupes, scores, and renders.
`--score` runs a Claude Haiku pass that categorizes every item, rates its
relevance, and drops the promos. Headlines are still the sources' own — the
writing pass is v2.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

.venv/bin/daily-dive sources               # what's configured
.venv/bin/daily-dive run --offline         # build from fixtures, no network
.venv/bin/daily-dive run                   # build from live feeds
.venv/bin/pytest -q                        # 57 tests, no network, no cost
```

Output lands in `site/` — `index.html` plus a dated permalink under `site/issues/`.

Useful while iterating:

```bash
daily-dive run --source reefbuilders --limit 5    # one feed, small
daily-dive run --offline --out site/preview       # scratch output, gitignored
daily-dive run --score --limit 20                 # with the AI pass (costs money)
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

### Watching the pages that have no feed

Probing settled a question: **the top of the ownership chain publishes no feed.**
`bertramcapital.com` and `apetlife.com` 404 on every conventional feed path and
advertise nothing on their `/news` pages. Same for Iwaki (including its IR page),
TUNZE, SICCE, Abyzz, Royal Exclusiv, and Pan World. These are hand-maintained
HTML pages.

That matters because a Bertram exit or add-on acquisition would move BRS,
EcoTech, Neptune, and AquaIllumination simultaneously — the highest-leverage
single event in the map, and there's no feed for it.

The options, none of them free:

1. **Rely on trade press.** An Aperture-scale transaction gets covered. Costs a
   day or two of latency on an event that happens maybe twice a year.
2. **Change-detection on those two specific pages.** Fetch daily, hash the
   content, flag a change. This is scraping, and it breaks the RSS-only rule —
   but narrowly: two known URLs, once a day, robots respected, linking to their
   page rather than reproducing it. If this is ever turned on, it belongs in
   `site/about.html` explicitly, not done quietly.
3. **Ask.** Same play as Reef2Reef.

Currently option 1. Ownership changes are rare enough that a day's latency costs
little, while quietly becoming a scraper would cost the posture that makes
everything else here defensible.

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
  entities.py         brand -> owner, no model calls
  score.py            v1: the Haiku scoring pass
  pricing.py          token and cost accounting
  render.py           Issue -> HTML
  cli.py              entry point
templates/            Jinja
prompts/              versioned prompt files — frozen, cacheable prefixes
tests/fixtures/       synthetic feeds; also the eval set for later prompt work
site/                 generated output, served by GitHub Pages
```

`dailydive.sqlite3` is committed on purpose: it's the dedupe memory and the
archive, it's free, and it versions both for free.

## Roadmap

| | |
|---|---|
| **v0** ✅ | Pipeline with no AI. Fetch, dedupe, render, full attribution. |
| **v1** ✅ | Haiku pass scores and categorizes every item. Sections get real. |
| **v2** | Clustering + Sonnet writes the issue + a grounding check. The Techmeme format proper. |
| **v3** | RSS out, then email from `mail.theloneaquarist.com`. |

Ship each one fully before starting the next. The temptation is to start at v2.

## The scoring pass (v1)

`daily-dive run --score` sends every item through Claude Haiku 4.5, which
returns structured output — a category from the closed enum, a relevance score,
a promo flag, and a one-clause gist — and drops everything below the relevance
threshold before the page is rendered.

Design notes worth knowing before editing `dailydive/score.py`:

- **The system prompt is a frozen prefix.** It's loaded verbatim from
  `prompts/score.system.md` and marked cacheable; per-item data goes in the user
  turn. Interpolating anything per-request — a date, a run id — would invalidate
  the cache on every call and quietly multiply cost. Haiku's cache minimum is
  4096 tokens, so watch `cache hit` in the run's cost line rather than assuming.
- **Structured output, not prose parsing.** `messages.parse()` validates against
  a pydantic schema, so a malformed reply raises here instead of causing a
  mystery three stages later.
- **The source's `category_hint` is deliberately withheld** from the model. The
  hint says where an item came from, not what it is; showing it just invites a
  rubber stamp.
- **Entity context is supplied, not recalled.** Detected companies come from the
  deterministic matcher, so the model reasons about ownership it was handed.
- **Invented uids are dropped.** A hallucinated uid would attach a score to the
  wrong story, so unmatched entries are discarded rather than guessed at.
- **Unscored items don't publish.** An unscored item is one nothing has judged.

Every run prints its own token and cost accounting. If `cache hit` reads 0%
across repeated runs, something is invalidating the prefix.

## Cost

Target is a few dollars a month: GitHub Actions and Pages are free on a public
repo, and the model spend is ~150 short classification calls plus one long
writing call per day. v0 spends nothing at all — there are no model calls in it.

## Note on sandboxed environments

Cloud sessions with `Trusted` network access can reach package registries but
not arbitrary sites, so live feed fetching returns 403 at the proxy. Use
`--offline` there, and run live fetches locally or from an environment with
wider network access.
