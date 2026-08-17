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

`prompts/` holds one file and no changelog, so a change in output quality cannot
be attributed to a change in instructions. Scores are not persisted either
(`store.py` keeps a seen log and a published log, not relevance), so re-rendering
an old issue costs a fresh scoring pass and past judgements cannot be revisited.

---

## Part four — what to build next

In this order, because each step makes the next one measurable.

### 1. Label a hundred items by hand

Take 100 real items from past runs and score each 0–1 the way it *should* have
been scored. Roughly two hours. Store it as a fixture next to
`tests/fixtures/preview-issue.json`.

This is the highest-value thing on the list, and it is worth being clear about
why. Every open question right now — is the bridge rule holding, is 0.45 the
right threshold, would a better model help, did that prompt edit improve
anything — is the same question in different clothes, and all of them are
unanswerable without a labelled set. It also forces the editorial standard
("depth, not marketing fluff") to become specific, which is useful even if no
model is ever run against it.

Measure: agreement with the model, and where the disagreements cluster.

### 2. Compare models on that set

Run Haiku and Sonnet against the labelled items. Now the model choice is a
decision with evidence attached, and the answer will be interesting either way —
including if it turns out the cheap model is fine, which is the outcome the
current architecture is betting on.

### 3. Build the grounding check on a small prose experiment

Two or three items, a generated summary, then a verification pass asking whether
each sentence is supported by the source item. Keep it small; the point is to
watch it fail, not to ship prose.

### 4. Version the prompts, persist the scores

A changelog in `prompts/`, and relevance stored in `store.py`. Together they
make it possible to diff issue quality across prompt versions instead of
recalling how it felt.

---

## The shortest version

- Decide early which model sees which data; that is the architecture.
- Make bad output unrepresentable rather than discouraged.
- Debug reasoning patterns, not individual wrong answers.
- Put invariants in the type system; put procedures in prose; do not confuse them.
- Assume confident completion claims are sometimes false, and build the check
  that does not depend on them.
- A system with no quality measurement can only be argued about.
