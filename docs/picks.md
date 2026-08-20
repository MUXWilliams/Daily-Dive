# Editor's picks — submitting your own story

How a story you found by hand gets into the newsletter: the form, what each
field is for, where it lands on GitHub, and how the Friday build picks it up.

**The form:** <https://claude.ai/code/artifact/ea28b78f-fba2-4908-ad84-5ba5e47c9b79>

---

## Why this exists

The crawler is locked out of places worth reading. Reef2Reef and Humble.Fish
return 403 to every feed shape; plenty of trade sites and product pages publish
no feed at all. Getting past the first would mean forging a User-Agent, which
this project has refused to do, and the second cannot be solved that way at all.

A person reading those sites and citing what they found is not a crawler. Picks
are that door. You read something during the week, file it, and Friday's build
drains the bucket.

**A pick outranks the model.** It joins the pipeline *after* scoring, so the
scorer can never drop a story you deliberately chose, and it is prepended to
the list so it survives near-duplicate collapsing and leads its section.

That is also why the form is strict. A pick reaches the page without the scorer
ever seeing it, so the form is the only gate it passes through — and the
attribution rules do not relax for the editor.

---

## Step 1 — fill in the form

Open the link above on a phone or a laptop. Nothing is submitted from the page
itself; it builds correctly-formatted text and you copy it out.

### Headline — required

The story's own headline, as the outlet wrote it. Not your summary of it, and
not a rewrite. This is the linked text a reader clicks.

### Link — required

The URL of the story. Must start with `http://` or `https://`.

Link the **specific story**, not the site's front page. The permalink is what
makes the credit meaningful and what a reader needs to actually get there.

### Outlet — required

**The site that published it — never a person, and never a username.**

This is the credit line on the page, and it is the field most likely to cause
real harm if you get it wrong. A Reef2Reef thread is credited "Reef2Reef", not
the member who posted it. Forum members did not ask to be published, and a
privacy mistake is not undoable once it is on a public page *and* in git
history.

There is no author field on the form at all, deliberately, and a test asserts
one can never be set on a pick.

The form offers chips for the outlets you use most — Reef2Reef, Humble.Fish,
SoCaliReefs, BulkReefSupply.com — which fill the outlet and pre-select a
sensible category. You can type anything else.

> **On storefront picks.** BulkReefSupply.com is kept as a chip but it is the
> exception. A pick skips the scorer, and therefore skips the promo filter —
> the thing keeping the rest of the issue free of marketing. So the bar is
> higher: a storefront pick has to be genuinely impactful to the community, not
> merely new stock.

### Category — required

One of the six, and it must match exactly:

| Category | What belongs there |
|---|---|
| `Community` | Forum threads, build logs, hobbyist discussion, videos |
| `Industry & Products` | Releases, company news, trade shows, recalls |
| `Husbandry & Science` | Technique, disease, chemistry, published research |
| `Livestock & Corals` | New morphs, aquaculture, collection news |
| `Wild Reefs` | Bleaching, oceanography, conservation, trade regulation |
| `Events` | Frag swaps, club meetings, shows |

The category decides which section the pick leads. It also decides its colour.

### Why it matters — optional, ≤40 words

The gist that appears under the headline. Same 40-word ceiling every other item
obeys — a summary, never a replacement for reading the source.

Leave it blank if the headline says everything. A pick with no gist renders
fine.

Over 40 words and the pick is rejected with a note telling you the count.

### Published — optional

`YYYY-MM-DD`. The date the story was published, not the date you filed it.

Blank means today. Anything else unparseable is rejected with a note.

### Industry beat — optional

Only meaningful for `Industry & Products`. Sets the small orange tag on the
item — Ownership, Leadership, Distribution, Product, Safety, Manufacturing,
Financial. See [`docs/industry-brief.md`](industry-brief.md) for what each one
means and the language rules that go with them.

---

## Step 2 — get it onto GitHub

The form has a **Copy** button. It produces text that looks like this:

```markdown
### Headline

Fluval unveils a new gyre pump at MACNA

### Link

https://www.reef2reef.com/threads/example.123456/

### Outlet

Reef2Reef

### Category

Industry & Products

### Why it matters

First gyre from Fluval, and the mounting looks like it fits existing brackets.

### Published

2026-08-19

### Industry beat

Product
```

Then, on GitHub:

1. Open **Issues → New issue** on `MUXWilliams/Daily-Dive`
2. Paste the copied text as the body
3. Give it any title you like — **the title is ignored**; the headline comes
   from the `### Headline` section
4. **Add the `pick` label.** This is not optional. The build only looks at open
   issues carrying that exact label
5. Submit

The artifact cannot open GitHub for you — a published artifact is sandboxed and
cannot navigate to another site — so copy-and-paste is the flow by design.

**You do not need the form.** It exists to get the formatting right and to
count your gist words. An issue you type by hand with the same `### Field`
headings works identically.

### What GitHub is doing here

GitHub Issues is the database. That is the whole trick:

- **Open means pending.** Closing an issue is how a pick leaves the bucket, so
  "not yet published" needs no extra state anywhere.
- **It is free and it already exists.** The repo is public, which is what makes
  Actions and Pages free.
- **It has a UI on every device**, which a static site cannot have, because a
  static site cannot accept a form POST.

The cost of a public repo is that **anyone can open an issue on it**. So only
issues opened by an allowlisted account become items — currently `muxwilliams`
and `muxxworx`. Anyone else is ignored in silence: no error, no reply. A
stranger filing on a public repo should not learn anything from how the build
responds.

---

## Step 3 — what the Friday build does

On a publishing run, after scoring and before rendering:

1. **Reads the bucket** — open issues labelled `pick`, from allowlisted authors.
2. **Parses each one** into an item, applying every check above.
3. **Drops anything already published.** Checked against what actually reached a
   page, not what was merely fetched.
4. **Prepends them** to the scored items, so a pick leads its section.
5. **Collapses near-duplicates.** If the crawler also found your story, the two
   merge and the pick survives as the one that runs — the crawled version
   becomes its "+1 similar" credit.
6. **Renders, publishes, and then answers the issues.**

### If it ran

The issue is **closed** with a comment naming where it landed:

> Published in the August 21, 2026 issue under **Industry & Products**.
>
> https://www.theloneaquarist.com/issues/2026-08-21.html

Only picks that survived to the final page are closed. A pick merged away by
duplicate collapsing did not run, and telling you it did would be a lie you'd
find out about on Friday.

### If it was rejected

The issue is **left open** with a comment saying why, so you can fix it and it
gets retried next week. The reasons, verbatim:

| Comment | Fix |
|---|---|
| `This is missing a headline, a link, an outlet.` | Add the missing `### Field` sections |
| `'...' isn't a usable link — it needs to start with http:// or https://.` | Full URL including the scheme |
| `The gist runs to 58 words and the ceiling is 40. Trim it and reopen this.` | Cut it down |
| `I don't recognise the category 'Gear'. It has to be one of: ...` | Use one of the six exactly |
| `I couldn't read '19/08/2026' as a date. Use YYYY-MM-DD, or leave it blank for today.` | `2026-08-19` |
| `This one already ran in an earlier issue, so I've left it out.` | Nothing to fix — close it |

### If a partial run happens

A `--source` or `--limit` run never publishes, so it never closes your issues
and never marks anything as published. Picks stay in the bucket. The log says
so explicitly.

### If you file nothing

Nothing happens. An empty bucket is the normal case and costs the build nothing.

---

## When picks stop appearing

**The most likely cause is the author allowlist, and it fails silently by
design.** An unrecognised author is ignored with no reply — which is correct
behaviour for a stranger and indistinguishable from an empty bucket when it is
you.

This has already happened once: the repo lives under `MUXWilliams`, but the
account that actually files issues is `muxxworx`, and assuming the owner and
the author were the same login cost the first real pick.

So, in order:

1. **Is the account in the allowlist?** `AUTHORS` in
   [`dailydive/picks.py`](../dailydive/picks.py). Adding one is a reviewable
   one-line diff, deliberately — not a config change nobody sees.
2. **Is the label exactly `pick`?** Not `picks`, not `Pick`.
3. **Is the issue open?**
4. **Check the run log** for `N pick(s) accepted, M rejected`. If it says
   `ignored N pick issue(s) from non-allowlisted accounts`, it is item 1.

---

## The rules that do not bend

- **The outlet is a site, never a person.** No author field exists, and a test
  asserts one can never be set on a pick.
- **Every pick needs a resolvable URL.** `Item` cannot be constructed without
  one, and `assert_attributable` re-checks at the publish boundary.
- **Gists cap at 40 words**, the same ceiling every scored item obeys.
- **Only allowlisted authors**, because an issue tracker on a public repo looks
  like a private inbox and is not one.

All four are enforced in code rather than by convention. The relevant tests
live in `tests/test_pipeline.py`; the parsing and validation live in
[`dailydive/picks.py`](../dailydive/picks.py), which is worth reading before
changing any of this.
