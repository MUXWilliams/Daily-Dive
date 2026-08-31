# Getting the site into Google

Written the week the question was asked, because the honest answer has two
halves and only one of them is code.

The site had been live for weeks with **no `robots.txt` and no `sitemap.xml`**,
and **not one page title contained the words "The Lone Aquarist"** — the exact
phrase someone searching for this publication would type. Both are fixed. The
rest is a Search Console job that only the domain's owner can do.

## What the repo now does on its own

`dailydive/seo.py` writes two files on every publishing run, immediately after
the archive index is updated:

| File | What it is |
|---|---|
| `site/robots.txt` | Static. Allows everything, names the sitemap, carries the delisting contact. |
| `site/sitemap.xml` | Generated from `site/issues/index.json`. |

The sitemap matters more than it looks. The dated permalinks at
`/issues/YYYY-MM-DD.html` are linked from **exactly one page**, `archive.html`.
A crawler that never reaches the archive never learns a single back issue
exists. The sitemap is how Google is told directly.

Three deliberate choices in there:

- **`lastmod` only where a real date is known.** Issue pages and the front page
  have one; `about.html` and `subscribe.html` do not. Inventing a timestamp to
  make the file look complete is telling a crawler something untrue.
- **No `changefreq`, no `priority`.** Google has said in public that it ignores
  both. A field nobody reads is a field that drifts without anyone noticing.
- **Generated, never hand-maintained.** The same reason `about.html` became a
  template after weeks of naming the wrong publication.

It sits inside the publish gate. A `--source` or `--limit` run has not
published anything, and a sitemap is a claim about what is live.

## What only you can do

None of this is code, and none of it can be done from the build container —
the egress proxy blocks `theloneaquarist.com` outright, so nothing here was
verified against the live host.

### 1. Add the site to Google Search Console

<https://search.google.com/search-console>

Choose a **Domain property** (`theloneaquarist.com`), not a URL-prefix one.
Verification is a TXT record at the registrar, and in exchange it covers the
apex and `www`, `http` and `https`, in one property. A URL-prefix property
would need a separate entry for each, and this site serves `www` via the
`CNAME` file while the apex redirects — two properties for one site.

The alternative is an HTML verification file committed into `site/`. It works
and it needs no DNS, but it makes the verification token part of a public
repository, and it only ever verifies the one prefix.

### 2. Submit the sitemap

Search Console → Sitemaps → enter `sitemap.xml`. Once. Google re-reads it on
its own afterwards; there is nothing to re-submit each Friday.

### 3. Ask for the home page specifically

Search Console → URL Inspection → paste `https://www.theloneaquarist.com/` →
**Request indexing**. This is the one manual nudge worth spending. Do it for
the home page and the archive page; leave the issues to the sitemap.

### 4. Wait, then check what actually happened

`site:theloneaquarist.com` in Google tells you what is indexed. Expect **days
to a few weeks** for a new domain — this is normal and is not a sign anything
is broken. The Pages report in Search Console names the reason for anything
excluded, and that report is the only trustworthy answer to "why isn't it
showing up"; guessing at it is how people end up changing things at random.

### 5. Optional: Bing

<https://www.bing.com/webmasters> takes the same sitemap and can import the
Search Console property wholesale. Worth ten minutes, because it also feeds
DuckDuckGo.

## The part no sitemap fixes

Indexed is not the same as found. A brand-new domain with no inbound links
will be indexed and then rank for essentially nothing except its own name.

This is also an aggregator, which is the harder case: every item links out and
the unique text on the page is the gist and the section structure. Google is
explicitly wary of pages that mostly restate other people's headlines. What
this publication actually has that the hobby press does not is the primary
literature and the public-aquarium practice — the depth described in
`CLAUDE.md` under editorial direction. That is the differentiator to lean on,
and it is an editorial answer rather than a technical one.

The practical levers, in the order they are worth pulling:

1. **Links from places reefkeepers already are.** One post on Reef2Reef or a
   club's forum is worth more than any meta tag on this page. The `/subscribe`
   URL exists precisely so there is something ours to share.
2. **The archive accumulating.** Five issues is a thin site; fifty is a
   reference. Nothing to do here but keep the Friday run green.
3. **RSS**, still open in `CLAUDE.md`. Feed readers and aggregators are a
   distribution path that does not depend on ranking at all.

## What was deliberately not done

- **JSON-LD structured data.** `NewsArticle` markup on an aggregator's digest
  page claims each issue is a news article this site wrote, which it is not —
  rule 1 of the project is that this credits other people's reporting rather
  than producing its own. The rich-result payoff for a link digest is close to
  zero, and the claim is false.
- **A `google-site-verification` meta tag** in `issue.html.j2`. It would put a
  Search Console token in every rendered page in a public repository, to buy
  verification that a DNS record does better.
