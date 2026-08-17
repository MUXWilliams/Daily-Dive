# Weekly Dive — brief for a new session

Read this first. It exists so a fresh conversation is useful immediately, and
so nothing important depends on a chat window that will eventually be
summarised or lost.

**The repo is the memory, not the conversation.** Decisions live in commit
messages, code comments and `docs/`. If you decide something here that matters
next month, write it down where the code is.

## What this is

A weekly news digest for the saltwater/reef aquarium hobby, published as
**Weekly Dive** under **The Lone Aquarist** at <https://www.theloneaquarist.com>.
It aggregates and credits other people's reporting; it does not write its own.

The editor is a reefkeeper, not a journalist by trade, and the project doubles
as a way to learn AI tooling end to end.

Repo: `MUXWilliams/Daily-Dive` (the name predates the rename; leave it).

## Hard rules — do not break these without being asked twice

1. **Every item credits and links its source.** Enforced in `models.Item`, not
   in a prompt. An item cannot be constructed without a source name, title and
   resolvable URL, and `assert_attributable` re-checks at render time.
2. **RSS/API only. Never scrape, never forge a User-Agent.** Reef2Reef,
   Humble.Fish, Reddit and YouTube's RSS all block us. The answer has always
   been to find a sanctioned door (the YouTube Data API, Bluesky RSS, IMAP
   newsletters, editor's picks) or to leave the source out.
3. **Never publish anything invented.** A page built from test fixtures once
   reached the live site carrying five fake headlines credited to Reef Builders
   and Reef2Reef. `.github/workflows/deploy.yml` now refuses to deploy any file
   containing `[SYNTHETIC]` or `example.invalid`.
4. **Picks never carry an author.** Forum members did not ask to be published.
   A pick credits the site — "Reef2Reef", never a username. There is no author
   field on the form and a test asserts an author can never be set.
5. **Untrusted inputs need allowlists.** The IMAP mailbox and the GitHub issue
   bucket both look private and are not. Senders and issue authors are
   allowlisted explicitly.
6. **Secrets come from the environment only** — never `sources.toml`, the
   database, a log line, or a page.

## The pipeline

`ingest → normalize → dedupe/archive → shorts filter → recency → score →
picks → collapse → resource → render → commit → deploy`

- **`sources.toml`** is the file edited most. 26 live sources: WordPress feeds,
  8 YouTube channels via the Data API, 10 Bluesky accounts, 2 IMAP newsletters,
  2 OpenAlex journal queries.
- **`score.py` + `prompts/score.system.md`** — one Haiku pass over every item,
  batched. Assigns a category, a 0–1 relevance, a promo flag and a ≤40-word
  gist. Below `DEFAULT_THRESHOLD` (0.45) is dropped. Costs roughly $0.07–0.10
  a run; total spend to date is well under a dollar.
- **`picks.py`** — the editor files stories the crawler cannot reach as GitHub
  issues labelled `pick`. They join after scoring (so the model cannot drop
  them) and are prepended (so they survive `collapse_similar` and lead their
  section).
- **`render.pick_resource` + `thumbs.py`** — one video is promoted out of its
  category to a **Resource** section at the foot of the page, with a still.
  Chosen by `RESOURCE_WORDS` (how-to / tips / tricks / mistakes) over raw score,
  because the section should hold something you'd come back to. Resource is
  deliberately **not** a `Category` member — the enum stays closed so the model
  cannot file things there.
- **`render.py` + `templates/`** — HTML only. No model writes prose. Every page
  renders from a template, including `about.html`, which was static until it
  spent weeks calling the publication by its old name.

## Conventions learned the hard way

- **Probe before configuring.** `daily-dive probe` reports HTTP verdict, entry
  count, newest-entry age and feed title. Six feeds parsed fine and had
  published nothing in years; three of those I had already recommended.
- **Never type an outlet name from memory.** `name` is the credit line. A web
  search for a channel ID confidently returned the name of the person behind
  it, which is not what the channel is called. Ask, or read it from the API.
- **The publish gate is duplicated on purpose** — in `cli._is_publishing_run`
  and in the workflow. A partial run (`--source`/`--limit`) must never record
  items as published or close pick issues. A test asserts both check the same
  conditions.
- **`daily-dive preview`** renders the template against a frozen fixture — free,
  offline, deterministic. Use it for any layout change. `--artifact` emits the
  form used for the hosted staging page.
- **Verify in a browser, not by assertion.** Playwright + Chromium are
  available (`/opt/pw-browsers/chromium`). Sticky headers were "fixed" twice
  before a real browser proved which change actually did it.
- **Don't pipe pytest through `tail` in a `&&` chain.** It masks the exit code,
  and a failing test gets pushed.
- **Images are fetched at build time and committed, never hotlinked.** A
  hotlinked thumbnail makes every reader's browser call Google just by opening
  the page, breaks the offline preview, and gets stripped by mail clients later.
  `thumbs.py` validates JPEG magic bytes and dimensions before writing, because
  YouTube answers a missing `maxresdefault` with a 200 and a 1 KB grey
  rectangle. Committed thumbnails are **never** deleted — back issues reference
  theirs forever.

## Where things are

| | |
|---|---|
| Live site | <https://www.theloneaquarist.com> |
| Back issues | `/archive.html`, permalinks at `/issues/YYYY-MM-DD.html` |
| Build | `.github/workflows/daily.yml` — Friday 10:00 UTC, plus manual |
| Redeploy | `.github/workflows/deploy.yml` — publishes committed `site/` as-is |
| Editorial rules | `prompts/score.system.md`, `docs/industry-brief.md` |
| Delivery plan | `docs/delivery.md` |
| Archive index | `site/issues/index.json` |
| Skills | `.claude/skills/preview/SKILL.md`, `.claude/skills/add-source/SKILL.md` |
| What this teaches | `docs/learning.md` |

Skills hold **procedures**; tests hold **invariants**. The hard rules above are
enforced in the type system and in CI on purpose — restating them as prose in a
skill would put them in a third place that can drift from the first two, which
is exactly how `site/about.html` ended up naming the wrong publication.

`dailydive.sqlite3` is committed and load-bearing: `items` is a *seen* log,
`published` is what actually reached a page, `http_cache` holds ETags. Losing
it means re-publishing old stories.

## Editorial direction

The differentiator is **depth**. The hobby press is largely marketing; this
carries primary literature, oceanography and public-aquarium practice
alongside the trade news. Two or three strong papers an issue — enough to be
distinctive, not so many it becomes a literature alert.

`prompts/score.system.md` applies a **subject test**, not an actionability
test. Read it before changing scoring behaviour; the reasoning for every rule
is written into it, including two real items that slipped through and why.

Community leads the issue: the reader is a hobbyist, and what other hobbyists
are building is why they opened it.

## Open threads

- **The bridge rule is untested at scale.** Marine-turtle poaching and
  farmed-salmon economics both got through by reasoning *to* reef relevance.
  The prompt fix has only faced a six-item run.
- **JZAR and ENSO** have no working feed. Both are in `probe.DISCOVER_TARGETS`
  for HTML autodiscovery. El Niño was asked for by name and nothing covers it.
- **Score persistence** — gists and relevance are not stored, so re-rendering
  an issue after a template change costs a scoring pass.
- **Seen vs published for the crawler** — items dropped under the old scoring
  prompt can never be reconsidered.
- **Delivery (v3)** — RSS, then email. See `docs/delivery.md`; the sequencing
  decision is already made.
