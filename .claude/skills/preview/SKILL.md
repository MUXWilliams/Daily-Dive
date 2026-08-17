---
name: preview
description: Render the issue page against the frozen fixture and publish it as a hosted staging artifact for review on a phone or desktop. Use whenever a change touches templates/, the stylesheet, or render.py, and whenever the editor asks to see how something looks — "show me", "what does it look like", "make me an artifact", "staging page". Free, offline, deterministic; no model calls.
---

# Staging preview

The design loop for this project. `daily-dive preview` renders the real template
against a frozen fixture — 24 real items from the first published issue plus two
clearly-marked synthetic ones so the Events and Elsewhere sections have
something to draw. No network, no model calls, no cost, and the same output
every time, so a stylesheet change is the only thing that moves.

Never wait for Friday and never spend a scoring pass to look at a layout.

## The procedure

### 1. Render

```
uv run daily-dive preview --artifact <scratch>/staging.html
```

`--artifact` emits the form a hosted artifact needs: no doctype, head or body
wrapper, and no canonical or OpenGraph tags (those name the live site and would
be wrong on a staging page). Assets inline as data URIs by default, which is
what makes the page render correctly on a phone rather than showing a broken
masthead.

Plain `uv run daily-dive preview` writes `site/preview/index.html` instead —
gitignored, and fine for a quick local look.

### 2. Handle images the container cannot fetch

The container proxy returns **403 for `i.ytimg.com`** and most other external
hosts. That is the environment's network policy, not the site being down.

When the change involves an image that cannot be fetched here, pass a data-URI
stand-in to `render.render_issue(thumb=…)` at publish time — a flat card that
**says on its face that it is a placeholder**. Never write a fabricated image
into `site/`.

> `site/` is what deploys. A made-up file sitting at a real video's id is the
> same shape as the fixture page that once reached the live site carrying five
> invented headlines. `deploy.yml` greps for `[SYNTHETIC]` and `example.invalid`
> and will refuse, but do not rely on the net.

### 3. Publish

Publish the file with the Artifact tool. **Keep the title and favicon stable
across redeploys** — the reader finds the tab by its icon, and a changed one
reads as a different page. Republishing the same file path keeps the same URL.

### 4. Verify in a real browser

Chromium is at `/opt/pw-browsers/chromium`; Playwright is configured to find it.
Do not run `playwright install`.

Check at **390×844** and **1280×900**:

- `document.documentElement.scrollWidth === clientWidth` — the body must never
  scroll horizontally
- any image holds its aspect ratio at both widths (measure the bounding box)
- `section.card > h2` reports `getBoundingClientRect().top === 0` after
  scrolling past its section start — the sticky check

### 5. Confirm nothing leaked

`git status --short` must show `site/` clean.

## Gotchas, each of which cost a debugging round

- **`overflow: hidden` on `section.card` kills the sticky headers silently.** It
  makes the card the sticky element's own scroll container, so the header sits
  still inside a box it already fills. Measured in Chromium: `-199.7px` with it,
  `0.0px` without. The `h2` rounds its own top corners instead.
- **Sticky must not be gated behind `prefers-reduced-motion`.** It is not
  animation — nothing accelerates or parallaxes — and gating it switched the
  feature off for everyone with Reduce Motion enabled, which on iOS is a great
  many people.
- **`inline_assets` needs a recursive glob.** Thumbnails live in
  `assets/thumbs/`, and the original non-recursive `glob("assets/*")` skipped
  them without saying so.
- **Verify in a browser, not by assertion.** The sticky headers were declared
  fixed twice before a browser was used to determine which change had done it.
