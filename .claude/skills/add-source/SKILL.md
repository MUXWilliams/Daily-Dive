---
name: add-source
description: Add a feed, YouTube channel, Bluesky account, or journal query to sources.toml the safe way — probe it first, read the outlet's real name rather than typing it from memory, then prove it with a limited live run. Use whenever the editor wants a new source added, a dead one parked, or an existing entry changed.
---

# Adding a source

`sources.toml` is the file edited most in this project, and it has the worst
failure history. Two failures in particular are why this procedure exists:

- **Six feeds parsed cleanly and had published nothing in years.** Three of them
  had already been recommended on the strength of "the feed works."
- **A web search for a channel id returned the name of the person behind the
  channel**, which is not what the channel is called. `name` is the credit line
  that appears on the page.

## The rule that comes first

**RSS/API only. Never scrape, never forge a User-Agent.** Reef2Reef,
Humble.Fish, Reddit and YouTube's per-channel RSS all block this crawler. The
answer has always been to find a sanctioned door — the YouTube Data API, Bluesky
RSS, IMAP newsletters, editor's picks — or to leave the source out. A block is
information, not an obstacle.

## The procedure

### 1. Probe before configuring

```
uv run daily-dive probe <url>
uv run daily-dive probe --discover        # ask known sites what they advertise
uv run daily-dive probe --markdown        # table, for pasting into an issue
```

The probe reports HTTP verdict, entry count, **newest-entry age**, and feed
title. The newest-entry age is the field that matters and the one that is easy
to skip past.

### 2. Read the verdict honestly

- **No feed** → it does not go in. See the rule above.
- **Newest entry over 60 days** → *not automatically dead.* CORAL Magazine is a
  print bi-monthly and a month of silence is its normal rhythm; its entry in
  `sources.toml` carries a comment saying so, precisely so nobody parks it on a
  quiet month. Check the publisher's actual cadence before deciding.
- **Parses but is all promotion** → the scorer's promo flag will handle some of
  it, but a source that is purely storefront output should not be added. Store
  and marketing links were considered and deliberately left out unless something
  is very impactful to the community.

### 3. Never type the outlet name from memory

`name` is how the outlet is **credited on the page**. Take it from the feed
title, or from the API response, or ask the editor. Do not take it from a web
search and do not reconstruct it from a URL.

Related, and non-negotiable: **an editor's pick credits the site, never an
individual.** Forum members did not ask to be published. A pick from Reef2Reef
is credited "Reef2Reef", not a username, and there is no author field on the
form.

### 3b. Newsletter sources need three steps, not one

The crawler cannot reach some outlets, but they all run newsletters. That path
is: sign up, get it labelled, then configure it.

1. Sign up with **`theloneaquarist+feeds@gmail.com`**.
2. A Gmail filter matches that plus-address and applies the label **`digest`**.
   One filter covers every future subscription, which is why it beats a filter
   per sender.
3. Add the `[[source]]` block with the sender in `senders`.

**The plus-tag and the label are different things and are easy to conflate** —
`+feeds` is what you type into someone's signup form, and Gmail leaves it in the
`To:` header for the filter to match. `digest` is the label that filter applies,
and it is the only thing the code reads: the source URLs are
`imap://imap.gmail.com/digest`, and the IMAP search is `SINCE` plus `FROM`,
never the `To:` header.

Miss step 3 and the mail arrives, gets labelled, and is silently ignored.

`daily-dive mailcheck` opens the mailbox read-only and lists the senders it can
actually see — use it to get the real sender address rather than guessing it
from the display name. `FORBIDDEN_MAILBOXES` refuses `inbox` and All Mail: this
repo is public and so is its Actions log.

### 4. YouTube specifics

- A channel id `UC…` maps to its uploads playlist by swapping the prefix to
  `UU…`; `playlistItems.list` is what the pipeline reads.
- `videos.list` costs **1 quota unit per call regardless of which parts are
  requested**, and accepts 50 ids at a time — so batching is the whole ballgame.
- Shorts are filtered at **180 seconds**, which is YouTube's own cutoff. There
  is no `isShort` field in the Data API; duration is the definition.
- The API key comes from the environment only. Never `sources.toml`, never the
  database, never a log line.

### 5. Write the entry, with what the probe saw

```toml
# Verified live: 10 items, newest "Treating Brown Jelly" (3 days).
[[source]]
id = "some-slug"          # stable; used in the DB and as --source <id>
name = "The Outlet"       # the credit line. Get this right.
url = "https://example.com/feed/"
type = "wordpress"        # wordpress | xenforo | youtube | reddit | generic
category_hint = "Husbandry & Science"
```

The comment recording the probe result is not decoration. Every existing block
has one, and it is why the file is still readable a month later — including the
entries that document *why something was kept* despite looking stale.

### 6. Prove it

```
uv run daily-dive run --source <id> --limit 5 --print-issue
uv run pytest
```

The limited run is a partial run, and the publish gate guarantees a partial run
will not record items as published or close pick issues. `--print-issue` shows
what the scorer kept, where it filed it, and how confident it was.

There are already tests asserting `sources.toml` parses and that ids are unique.
Do not pipe `pytest` through `tail` in an `&&` chain — it masks the exit code.

## The environment caveat

**In this cloud container the proxy blocks most external hosts**, so `probe`
will report 403 for sources that are perfectly healthy. That is the network
policy, not the site.

Do not read those 403s as dead feeds and do not start removing entries on the
strength of them. Probing for real means the `workflow_dispatch` path in
`.github/workflows/daily.yml`, whose probe step deliberately runs last so its
table lands at the end of the log.
