# Weekly Dive

A weekly news digest for the saltwater and reef aquarium hobby, published as
**Weekly Dive** under **The Lone Aquarist** at
[theloneaquarist.com](https://www.theloneaquarist.com).

It reads the feeds that matter — trade publications, YouTube channels, marine
science accounts, journal queries and a few newsletters — dedupes what overlaps,
scores it, sorts it into sections, and produces one scannable page every Friday.
Every item credits and links its source; this aggregates other people's
reporting and never writes its own.

The differentiator is depth. The hobby press is largely marketing, so this
carries primary literature, oceanography and public-aquarium practice alongside
the trade news.

*(The repo is named `Daily-Dive` and the crawler identifies as `DailyDiveBot`.
Both predate the rename to weekly and are deliberately left alone — publishers
may have allowlisted the user-agent string.)*

## What it does now

- **29 live sources**: 10 Bluesky accounts, 8 YouTube channels via the Data API,
  4 IMAP newsletters, 3 WordPress feeds, 2 OpenAlex journal queries, 2 others.
- **Scores every item** with one batched Claude Haiku pass — category, 0–1
  relevance, promo flag, ≤40-word gist. About **3¢ a run**.
- **Publishes** to GitHub Pages: a front page, a dated permalink per issue, an
  archive, and an about & sourcing policy page.
- **Emails** the issue to a Buttondown list, off by default and behind the same
  gate that decides whether a run publishes at all.
- **Accepts editor's picks** — stories the crawler cannot reach, filed as GitHub
  issues labelled `pick`.
- **Measures its own scoring** against a hand-labelled set of 128 real items.

No server anywhere. Actions runs the pipeline, Pages serves it, and total spend
to date is under a dollar.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

uv run daily-dive sources                  # what's configured
uv run daily-dive preview                  # render the frozen fixture, no network
uv run daily-dive run --offline            # build from fixtures
uv run pytest -q                           # the whole suite, offline and free
```

Output lands in `site/` — `index.html`, a dated permalink under `site/issues/`,
plus `archive.html`, `about.html` and `subscribe.html`.

Useful while iterating:

```bash
daily-dive run --source reefbuilders --limit 5   # one feed — a partial run never publishes
daily-dive probe <url>                           # test a candidate feed before adding it
daily-dive send --fixture --dry-run              # the exact email request, key redacted
daily-dive eval sheet --out sheet.html           # build the scoring labelling page
```

## The pipeline

```
ingest → normalize → dedupe/archive → shorts filter → recency → score →
picks → collapse → resource → render → commit → deploy → send
```

| Module | Job |
|---|---|
| `config.py` | `sources.toml` → `Source` |
| `ingest.py` | robots.txt, rate limiting, conditional GET |
| `normalize.py` | feed dialects in, one `Item` model out |
| `mailbox.py` | IMAP newsletters in — read-only, sender allowlist |
| `youtube.py` | Shorts detection by duration; there is no `isShort` field |
| `score.py` | the Haiku pass, batched 20 at a time |
| `picks.py` | editor's picks from GitHub issues |
| `thumbs.py` | the Resource video's still, fetched and validated at build time |
| `render.py` | `Issue` → page, permalink, archive, about, email |
| `deliver.py` | one POST to the mailing list — the only provider-specific file |
| `eval.py` | scoring quality, measured against hand labels |
| `store.py` | SQLite: seen log, published log, scores, HTTP cache |

`dailydive.sqlite3` is committed on purpose and is load-bearing: `items` is a
*seen* log, `published` is what actually reached a page, `scores` records what
the model decided **including what it threw away**, and `http_cache` holds ETags.

## Adding sources

Edit [`sources.toml`](sources.toml). One `[[source]]` block per feed.

**The rule: RSS/API only.** If a source has no feed, it does not go in. No HTML
scraping, no headless browser, and never a forged User-Agent to get past a
block. Reef2Reef, Humble.Fish, Reddit and YouTube's per-channel RSS all refuse
this crawler; the answer has always been a sanctioned door — the YouTube Data
API, Bluesky RSS, IMAP newsletters, editor's picks — or leaving the source out.

**Probe before configuring.** `daily-dive probe <url>` reports HTTP verdict,
entry count, newest-entry age and feed title. Six feeds once parsed cleanly and
had published nothing in years. A feed that parses is not a feed that publishes.

**Never type an outlet name from memory.** `name` is the credit line on the
page. Take it from the feed title or the API, or ask.

Full procedure in `.claude/skills/add-source/SKILL.md`.

## Attribution

The project depends on being a good citizen about this, so the rules live in the
type system rather than in convention or a prompt:

- `Item` cannot be constructed without `source_name`, `title`, a resolvable
  `url` and `published_at`. Entries missing any are dropped, never half-cited.
- `assert_attributable` re-checks at the publish boundary, so a bug upstream
  raises at build time instead of shipping an uncredited line.
- Gists are capped at 40 words and never replace reading the source.
- Every issue footer names every outlet that appears.
- **Picks never carry an author.** A pick credits the site — "Reef2Reef", never
  a username. Forum members did not ask to be published, and a test asserts an
  author can never be set on one.
- Headlines are reproduced as the outlet wrote them. The one exception is the
  *email subject line*, where a headline arriving in full caps is re-cased —
  all-caps is a spam signal, and a subject line is ours to compose.
- The fetcher honors `robots.txt`, identifies itself with a contact address,
  limits itself to one request per second per host, and sends conditional-GET
  headers so an ordinary run re-fetches almost nothing.

If you run something that appears here and want it removed, the address in
`dailydive/brand.py` reaches a human.

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
   page rather than reproducing it. If this is ever turned on, it belongs on the
   about page explicitly, not done quietly.
3. **Ask.** Same play as Reef2Reef.

Currently option 1. Ownership changes are rare enough that a day's latency costs
little, while quietly becoming a scraper would cost the posture that makes
everything else here defensible.

## Scoring, and how it is measured

`--score` sends every item through Claude Haiku 4.5 with structured output — a
category from a closed enum, a relevance, a promo flag and a gist — and drops
everything below the threshold before rendering.

Notes worth knowing before editing `score.py` or `prompts/score.system.md`:

- **The system prompt is a frozen, cacheable prefix.** Interpolating anything
  per-request would invalidate the cache and quietly multiply cost. Watch
  `cache hit` in the cost line rather than assuming.
- **The model never writes a URL.** Links are attached from source data on
  either side of the call, so a hallucinated link is unrepresentable rather than
  merely unlikely.
- **`category_hint` is withheld** from the model. It says where an item came
  from, not what it is; showing it invites a rubber stamp.
- **Invented uids are dropped**, so a hallucinated id cannot attach a score to
  the wrong story.
- **Unscored items don't publish.** An unscored item is one nothing has judged.

**Quality is measured, not asserted.** `daily-dive eval` scores 128 hand-labelled
real items and reports precision@20, rank agreement, and the two unambiguous
error classes — an item the editor called a lead that the model buried, and one
the editor would drop that the model ran. Reports land in
`docs/eval/<prompt-hash>.md`, one per prompt version, so "did that edit help" is
a diff rather than a recollection.

## Delivery

The page is the canonical artifact; the email is a copy of it. A refused send
turns the run red but never costs the issue its publication.

Sending is **off by default, including on the schedule** — scoring defaults on
for the weekly run and sending does not. A page can be redeployed; an inbox
cannot be un-sent.

Signup is the part that genuinely needs a server, which this project does not
have: a static site cannot accept a form POST, and the repo is public so
subscriber addresses can never live in it. Buttondown solves exactly that, and
`site/subscribe.html` is a redirect so the shareable URL stays ours. See
[`docs/delivery.md`](docs/delivery.md) for the reasoning.

## Roadmap

| | |
|---|---|
| **v0** ✅ | Pipeline with no AI. Fetch, dedupe, render, full attribution. |
| **v1** ✅ | Haiku pass scores and categorizes every item. |
| **v2** ✅ | Picks, archive, Resource section, scoring eval, email delivery. |
| **v3** | RSS out. `site/issues/index.json` is already the right shape for it. |
| **later** | A writing pass with a grounding check — the piece that would teach the most about where these models fail, and the one still unbuilt. |

## Cost

Under a dollar total to date. Actions and Pages are free on a public repo,
Buttondown is free under 100 subscribers, and a scoring run is about 3¢.

## Reading further

| | |
|---|---|
| Session briefing | [`CLAUDE.md`](CLAUDE.md) — read this first if you are picking the project up |
| What it teaches | [`docs/learning.md`](docs/learning.md) — what building this demonstrated about AI systems, including what went wrong |
| Delivery reasoning | [`docs/delivery.md`](docs/delivery.md) |
| Editorial rules | [`prompts/score.system.md`](prompts/score.system.md) |
| Procedures | `.claude/skills/` — the staging preview loop, and adding a source |

## Note on sandboxed environments

Cloud sessions can reach package registries but not arbitrary sites, so live
feed fetching returns 403 at the proxy — as do `i.ytimg.com` and
`docs.buttondown.com`. Use `--offline` and `preview` there, and run anything
live through the `workflow_dispatch` path in Actions.
