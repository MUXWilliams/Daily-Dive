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
  relevance, promo flag, ≤40-word gist. About **0.08¢ an item**, so a run costs
  what the week costs: 39 items in mid-August was 3¢, the 104-item run on
  28 August was 8¢.
- **Publishes** to GitHub Pages: a front page, a dated permalink per issue, an
  archive, an about & sourcing policy page, a subscribe page, and a generated
  `robots.txt` and `sitemap.xml`.
- **Takes signups on its own domain** — a Buttondown form embedded in the issue
  footer and on `/subscribe`, rather than an iframe or an off-site bounce.
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
plus `archive.html`, `about.html`, `subscribe.html`, `robots.txt` and
`sitemap.xml`.

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
| `archive.py` | the back-issue index — a JSON sidecar, not a scan of the HTML |
| `seo.py` | `robots.txt` and `sitemap.xml`, generated from that index |
| `pricing.py` | token accounting, so cost questions have answers |
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

- **The system prompt is a frozen prefix, marked cacheable.** Interpolating
  anything per-request would invalidate the cache and quietly multiply cost.
  It does not always *take*, though: at roughly 2,800 tokens the prompt sits
  under Haiku's 4,096-token cache minimum, and a measured run reported
  `cache hit 0%`. Read the number in the cost line rather than assuming the
  marking worked.
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

The file name is a hash of the prompt itself, so it changes exactly when the
prompt changes and never when it doesn't — nobody has to remember to bump a
version.

Three versions in, that has already earned its keep in both directions:

| Prompt | Top 20 | ρ | Leads buried |
|---|---|---|---|
| `bbd4b48d3628` | 19/20 | +0.38 | 9 |
| `7a3c49dadcca` | **20/20** | **+0.48** | 8 |
| `c2407f69c031` | 19/20 | +0.46 | 9 |

The middle one is the current best. The third was an attempted fix that made
things worse and was reverted — and the reports are why that is a sentence
rather than an argument. Two things it caught that no amount of reading the
prompt would have: quoting an item's *wrong* score as an example anchors the
model to that number, and inserting a thousand tokens above a working rule can
break it by displacement alone.

## Delivery

The page is the canonical artifact; the email is a copy of it. A refused send
turns the run red but never costs the issue its publication.

Sending runs **automatically on the Friday schedule**, alongside scoring. It was
opt-in until the first send had been proved end to end by hand — an inbox cannot
be un-sent, so that was worth doing once before letting it run unattended.

A **manual dispatch still has to tick `send`**, because a mid-week run by hand
is almost always a test.

Signup is the part that genuinely needs a server, which this project does not
have: a static site cannot accept a form POST, and the repo is public so
subscriber addresses can never live in it. Buttondown solves exactly that.

`site/subscribe.html` was a meta-refresh to their hosted page; it is a real page
now, carrying an embedded form that also sits in every issue footer. A reader
stays on this domain until the moment they submit. The **form embed rather than
the iframe** was deliberate — an iframe loads a third-party document on every
visit from every reader, subscriber or not, which would quietly undo the
decision to switch open and click tracking off. It also cannot be styled, and
theirs ships fixed at 220px with scrolling disabled. See
[`docs/delivery.md`](docs/delivery.md) for the rest of the reasoning.

## Being findable

`seo.py` writes `robots.txt` and `sitemap.xml` on every publishing run,
generated from `site/issues/index.json` rather than by scanning the output —
the same sidecar the archive page reads, and the one RSS will read next.

The sitemap is the part that matters. The dated permalinks are linked from
exactly one page, `archive.html`, so a crawler that never reaches it never
learns a back issue exists.

Three choices worth keeping:

- **`lastmod` only where a real date is known.** Issues and the front page have
  one; `about.html` and `subscribe.html` do not. Inventing a timestamp to make
  the file look complete tells a crawler something untrue.
- **No `changefreq`, no `priority`.** Google ignores both, and a field nobody
  reads drifts without anyone noticing.
- **Inside the publish gate.** A `--source` or `--limit` run has published
  nothing, and a sitemap is a claim about what is live.

The half that is not code — the Search Console property, verified by DNS rather
than by a token committed to a public repo — is in
[`docs/indexing.md`](docs/indexing.md), along with the honest note that indexed
is not the same as found. An aggregator whose unique text is a gist per item
ranks on inbound links and on the primary literature that makes it different,
not on markup.

## Roadmap

| | |
|---|---|
| **v0** ✅ | Pipeline with no AI. Fetch, dedupe, render, full attribution. |
| **v1** ✅ | Haiku pass scores and categorizes every item. |
| **v2** ✅ | Picks, archive, Resource section, scoring eval, email delivery, on-site signup, search indexing. |
| **v3** | RSS out. `site/issues/index.json` is already the right shape for it. |
| **later** | A writing pass with a grounding check — the piece that would teach the most about where these models fail, and the one still unbuilt. |

## Cost

Comfortably under a dollar to date. Actions and Pages are free on a public
repo, and Buttondown is free under 100 subscribers.

Scoring is the only line item, at roughly **0.08¢ an item** measured — so it
scales with the week rather than being a fixed figure. The 104-item run on
28 August cost about 8¢; a full 128-item eval pass measured $0.10. An earlier
claim of "3¢ a run" was accurate when issues were 39 items and simply aged.
`pricing.py` prints the real number on every run, which is the point of it.

## Reading further

| | |
|---|---|
| Session briefing | [`CLAUDE.md`](CLAUDE.md) — read this first if you are picking the project up |
| What it teaches | [`docs/learning.md`](docs/learning.md) — what building this demonstrated about AI systems, including what went wrong |
| Submitting a pick | [`docs/picks.md`](docs/picks.md) — the form, the GitHub issue, and how it reaches the page |
| Delivery reasoning | [`docs/delivery.md`](docs/delivery.md) |
| Getting indexed | [`docs/indexing.md`](docs/indexing.md) — robots, sitemap, Search Console |
| Editorial rules | [`prompts/score.system.md`](prompts/score.system.md) |
| Procedures | `.claude/skills/` — the staging preview loop, and adding a source |

## Note on sandboxed environments

Cloud sessions can reach package registries but not arbitrary sites, so live
feed fetching returns 403 at the proxy — as do `i.ytimg.com` and
`docs.buttondown.com`. Use `--offline` and `preview` there, and run anything
live through the `workflow_dispatch` path in Actions.
