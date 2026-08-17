# What this project teaches about AI systems

Weekly Dive exists for two reasons. One is to read it on a Friday. The other is
that building it end to end is a way to learn how these systems actually behave,
as opposed to how they are described. This file is the second reason, written
down: what the project has demonstrated so far, what it has not, and what to
build next to close the gap.

Written after roughly a month of real runs, at a total model spend well under a
dollar.

---

## Part one — what has been demonstrated

### Routing by cost is the architecture, not an optimisation

About 150 items arrive from the feeds each week. A cheap classifier
(`claude-haiku-4-5`, batched twenty at a time in `score.py`) reads every one and
assigns a category, a 0–1 relevance, a promo flag and a short gist. Everything
below `DEFAULT_THRESHOLD` is dropped, leaving roughly twenty items to render.

The lesson is not "use a small model." It is that **the decision about which
model sees which data is the main structural decision in an AI system**, and it
is made early, in the shape of the pipeline, not late, in a config value.
`pricing.RunSpend` tracks spend per stage precisely so that this stays a
measured fact rather than an assumption.

### Constrain the output space rather than asking nicely

Two mechanisms in this codebase do the same job in different ways:

- `Category` is a closed `StrEnum`. The scorer picks a member or the item is
  dropped. It cannot invent a bucket, because there is no way to express one.
- **The model never writes a URL.** Links are attached from the source data
  before and after the scoring call. A hallucinated link is not unlikely here;
  it is unrepresentable.

This is the most transferable idea in the whole project. Hallucination is not
defeated by better wording. It is defeated by removing the opportunity to make
the thing up. When a field matters, ask whether the model needs to author it at
all — usually it needs to *select*, and selection can be constrained in a way
that authorship cannot.

### Model failures are systematic, not random

The clearest example is the **bridge rule**, recorded in
`prompts/score.system.md`.

Two items got through scoring that should not have: marine-turtle poaching at
0.60, and farmed-salmon economics at 0.55. Neither is about reef aquaria. The
model was not confused — it was reasoning *toward* relevance: this illustrates
pressures on reef ecosystems, this informs understanding of the aquaculture
supply chain.

The fix that failed was adding nouns to an exclusion list; there are always more
nouns. The fix that worked was naming the **move**: phrases like *illustrates,
highlights, underscores, informs understanding of, is relevant to broader* are
the tell that a subject test has been replaced by a chain of association, and
anything reached that way scores 0.2 or below.

Generalisation: when a model gets something wrong repeatedly, look for the
reasoning pattern rather than the category of mistake. Patch the pattern.

### Prompts drift; type systems do not

This has now played out twice in this repo, with opposite outcomes.

**It held** for attribution. An `Item` cannot be constructed without a source
name, a title and a resolvable URL, and `assert_attributable` re-checks at
render time. There is no prompt anywhere instructing the model to credit
sources, because credit is not something the model is trusted with.

**It failed** for the about page. `site/about.html` was hand-written static HTML
sitting outside the template layer, so when the publication was renamed from
Daily Dive to Weekly Dive, the rename reached every generated page and not that
one. It told readers the wrong name and the wrong cadence for weeks. Nothing was
wrong when it was written — that is the instructive part. The fix was not to be
more careful; it was to render it from a template so `brand.py` is the only
place the name exists.

### Context is not memory

`CLAUDE.md` exists because a chat window is not durable state. It carries the
hard rules, the pipeline, the conventions and the open threads, and two tests
keep it honest: one asserts the rules are actually named in it, the other walks
every file path it references and fails if one has moved. A briefing that has
quietly gone stale is worse than no briefing, because it spends trust before the
reader finds out.

The general form: **in an agentic system, durable state lives outside the
context window, and whatever carries it needs a test.**

### Data becomes instruction, and that is an attack surface

Three places in this project where untrusted input reaches something that acts
on it:

- A workflow input containing `&` was interpolated straight into a shell
  command, where it was read as a background operator. The probe reported
  success in zero seconds having probed nothing. The same hole is a script
  injection vector; the fix was `env:` plus `set -f`.
- The IMAP mailbox looks private and is not — anyone can email it. Senders are
  allowlisted explicitly, and `mailcheck` must never read INBOX or All Mail.
- The GitHub issue bucket looks private and is not. Issue authors are
  allowlisted, and unrecognised authors are ignored silently.

Feed titles get the same treatment: `select_autoescape` was matching on the
final extension and `issue.html.j2` ends in `.j2`, so every feed title was being
written into the page unescaped for weeks. Anyone who can post to a syndicated
forum can put markup in a title.

---

## Part two — what my own mistakes demonstrated

This section is here because it is the most useful data the project has
produced, and it will not survive in commit messages alone.

Over the course of building this, the assistant (me) did the following:

- Told the editor the archive was working. It was not — `dailydive.sqlite3` was
  never persisted between runs, so every scheduled run started from an empty
  database. The claim was confident and wrong.
- Published a page built from test fixtures to the live site, carrying five
  invented headlines credited to Reef Builders and Reef2Reef. Cause: removing
  the `.gitignore` lines that prevented exactly this, then using `git add -A`.
- Misdiagnosed a DNS problem twice, including handing over an action list —
  check DNSSEC, republish the zone, contact support — that had to be retracted
  once it became clear the resolver had simply been throttled after ~30 queries.
- "Fixed" the sticky section headers twice before a real browser was used to
  determine which change had actually done it.
- Piped `pytest` through `tail` inside an `&&` chain, which masks the exit code,
  and pushed a failing test.

None of these are exotic. The pattern behind all of them is the same:

> **A language model will assert completion confidently, and cannot reliably
> detect when it is wrong about that.**

Which is why every countermeasure that stuck is one that does not route through
the model's own judgement:

| Failure | The check that replaced trust |
|---|---|
| Invented content reaching the site | `deploy.yml` greps for `[SYNTHETIC]` and `example.invalid` and refuses |
| Partial runs claiming to publish | The gate is duplicated in `cli._is_publishing_run` *and* the workflow, with a test asserting they agree |
| Layout claims | Playwright + Chromium, measuring `getBoundingClientRect().top` |
| Test claims | The exit code, unpiped |
| Uncredited items | `assert_attributable` raising at render time |

The useful mental model is not "is the model smart enough." It is **"what check
exists here that does not depend on the model being right."** Where the answer
is "none," that is where the next incident comes from.

---

## Part three — what has *not* been demonstrated

Being specific about the gaps matters more than the list of wins.

### There is no measure of scoring quality

There are 219 tests. Every one of them checks that the pipeline *works* — that
feeds parse, that items carry credit, that sections order correctly, that a
partial run does not publish. **Not one of them checks whether the scoring is
any good.**

That distinction — correctness versus quality — is the central one in applied AI
work, and this project currently lives entirely on one side of it. The bridge
rule sits in `CLAUDE.md` as an open thread for exactly this reason: it has faced
a six-item run and there is no way to know whether it holds at scale.

### No model has ever been compared against another

Everything is `claude-haiku-4-5`. There is no evidence about what a larger model
would buy, because there is nothing to measure against.

### No model has ever written prose here

The original plan's v2 — a write pass producing headlines and summaries, plus a
**grounding check** verifying that every generated sentence maps to a source
item — was never built. The plan described the grounding check as "the piece
that teaches you the most about where LLMs actually fail," and it is the one
piece missing. Shipping links-only was the right call for getting a product out;
it does mean the failure mode the whole design guards against has never been
observed in this system.

### Prompts are not versioned

`prompts/` holds one file and no changelog, so *what* changed between two
versions has to be read out of the git history rather than stated.

Scores were not persisted either, which was the worse half of this and is now
fixed — see the correction under *What to build next*. Until it was, past
judgements could not be revisited at all and re-rendering an old issue cost a
fresh scoring pass.

---

## Part four — what to build next

In this order, because each step makes the next one measurable.

> **Corrected after starting step 1.** This list originally put score
> persistence last, as a convenience. That was wrong, and finding out why is
> itself the lesson. Relevance was never stored anywhere — `items` is a seen
> log, `published` is what shipped, and no table held a score. So agreement
> could not be measured retroactively at all, and worse, *no measurement could
> ever be compared to another one*: a prompt edit had nothing to be diffed
> against. Persistence is not step 4, it is the precondition for step 1 being
> repeatable rather than a one-off. It is now built, and this list is renumbered
> to say so.
>
> The second surprise: the seen log held 808 items but only ~128 had ever
> reached the scorer. The rest were dropped by the recency filter before any
> model saw them — YouTube returns fifty videos per channel on first fetch,
> most of them years old. Sampling naively would have spent an hour labelling
> items nothing had judged. **Check what your eval corpus actually is before
> labelling it.**

### 0. Persist what the scorer decided — done

`store.scores`, keyed by `(uid, prompt_hash, model)`, written for every scored
item *including the ones below threshold*. The drops are the half that hides
the expensive mistakes, and the half that cannot be recovered later.

`prompt_hash` is derived from the prompt file rather than hand-maintained,
because a version string is a thing somebody forgets to bump on the one edit
that mattered.

### 1. Label the eligible set by hand

`daily-dive eval sheet` builds a labelling page from the items that plausibly
reached the scorer; `daily-dive eval report` scores them and compares. Four
ordinal buckets — Lead, Include, Borderline, Drop — rather than a 0–1 number,
because a human's 0.6 at item 10 does not mean their 0.6 at item 90.

**The sheet never shows the model's score.** A number in view decides the label
before you have finished reading, and an anchored label is an expensive way to
confirm what the model already thought. A test asserts no relevance value
appears in the rendered page.

This is the highest-value thing on the list, and it is worth being clear about
why. Every open question right now — is the bridge rule holding, is 0.45 the
right threshold, would a better model help, did that prompt edit improve
anything — is the same question in different clothes, and all of them are
unanswerable without a labelled set. It also forces the editorial standard
("depth, not marketing fluff") to become specific, which is useful even if no
model is ever run against it.

The output that matters is not an accuracy percentage. It is the list of items
you would have led with and the model discarded — those never reach a page, so
no amount of reading the published issue would reveal them.

**What labelling revealed, before a single item was scored.** The editor admits
76% of these items (90 of 118, borderline excluded); the pipeline ships about
13% of what it fetches. That gap is not straightforwardly a scorer failure,
and working out why is the lesson:

> The labels answer *does this belong in an issue at all* — admissibility. The
> threshold answers *how many fit* — rationing. They are different questions,
> and a report that compares an admissibility label against a rationing decision
> produces a false-negative list of fifty items that is technically correct and
> practically useless.

So the report only counts what the labels genuinely license. An editor **Drop**
that scored above threshold is an error, full stop. An editor **Lead** that
scored below it is an error, full stop — a lead is not a marginal call. An
**Include** below threshold is listed but never counted, because it may be
correct rationing. And the production question is ranking rather than
classification: of the twenty items that would actually ship, how many did the
editor want? That is `precision@20`.

**The eval design was wrong until the labels existed to expose it.** Which is
the general lesson about quality measurement: you cannot design the metric
before you have the labels, because the labels are what tell you which question
you actually asked.

Two findings were legible in the labels alone, and both are config decisions
rather than prompt ones — a distinction worth making before touching either:

- `openalex-coral`: **15 of 16 admitted.** The editor wants essentially every
  journal paper, which is exactly what the publication claims to differentiate
  on.
- `ubcoceans-bsky`: **10 of 26.** The largest single source and the least
  admitted. That is fixed in `sources.toml`.

### 2. Compare models on that set

Run Haiku and Sonnet against the labelled items. Now the model choice is a
decision with evidence attached, and the answer will be interesting either way —
including if it turns out the cheap model is fine, which is the outcome the
current architecture is betting on.

### 3. Build the grounding check on a small prose experiment

Two or three items, a generated summary, then a verification pass asking whether
each sentence is supported by the source item. Keep it small; the point is to
watch it fail, not to ship prose.

### 4. Version the prompts

A changelog in `prompts/`. The hash already identifies *which* prompt ran; a
changelog says *what changed and why*, which is the half a hash cannot carry.

---

## The shortest version

- Decide early which model sees which data; that is the architecture.
- Make bad output unrepresentable rather than discouraged.
- Debug reasoning patterns, not individual wrong answers.
- Put invariants in the type system; put procedures in prose; do not confuse them.
- Assume confident completion claims are sometimes false, and build the check
  that does not depend on them.
- A system with no quality measurement can only be argued about.

---

## Appendix — reading the first real eval

The first report (`docs/eval/bbd4b48d3628.md`) is a good worked example of why
a quality measurement is worth building, because almost every conclusion I drew
from the summary numbers was wrong until I looked at the individual items.

**The headline was good and slightly misleading.** precision@20 was 19/20 and
precision@10 was 10/10: what actually ships is what the editor wants. But rank
agreement was only +0.38, and 27 of 128 items scored *exactly* 0.00. A scorer
emitting a hard zero for 21% of its input is not grading, it is rejecting — and
that pattern is invisible in any summary statistic.

**My first diagnosis was wrong.** From the report alone the picture looked like
an inversion: World Wide Corals' hashtag-stacked clip titles scoring 0.50–0.65
while a Frontiers paper scored 0.00. "The scorer prefers marketing to
substance" is a tidy story and it was not the story. Pulling the gists and the
body text showed three unrelated causes:

1. **A thin body produced a zero regardless of the title.** The gists said so
   outright: *"Link only, no content provided to evaluate"*, *"A headline-only
   post... Cannot evaluate the topic."* The model was obeying the prompt's own
   rules — "content that is only a link" and "do not guess" — on feeds whose
   body happens to be a bare URL or a truncated WordPress excerpt. The prompt
   never said a headline is content. **This was the single largest cause and it
   was a prompt bug, not a judgement failure.**

2. **The out-of-scope list was narrower than the editorial direction.** Kelp
   forests, a seahorse-evolution paper and Black Sea fish nutrition were all
   correctly excluded *by the rules as written* — and all three were labelled
   leads. The seahorse case was a straight error (seahorses are marine
   ornamentals; they are sold in every livestock catalogue the reader browses)
   and is fixed. The other two are an editorial question, not a bug.

3. **The "marketing" false positives were four clips of one video.** All four
   share a single body description, so the model scored the same text four
   times; the editor discriminated between them on their titles, marking one a
   lead and two drops. That is a deduplication problem as much as a scoring one.

**The lesson.** Summary statistics told me something was wrong and pointed at
the wrong cause. The false-negative list — items, gists, body text, read one at
a time — told me what was actually happening. Build the list, then read it; do
not stop at the percentage.

A related finding worth keeping: one item's stored body was
`&#8230; The post How Can The Reef Keeping Hobby Grow? appeared first on
Reefs.com`, which is WordPress boilerplate with an undecoded HTML entity. That
is an ingestion bug in `normalize.py`, discovered only because the eval put a
model's confused gist next to the text that confused it.
