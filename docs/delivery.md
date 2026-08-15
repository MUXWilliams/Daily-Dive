# Delivery — how an issue reaches a reader

Notes on the v3 milestone, written while evaluating an open-source newsletter
architecture guide against what this project actually is. Recorded here rather
than decided in a chat window, because the choice has a running cost and the
reasoning is the part worth keeping.

## What the pipeline already does

Ingest, score, and render are done. `render.write_issue` produces
`site/index.html` plus a dated permalink from one `Issue` object, and the same
object can render to any other format. Delivery is not a content problem — the
content exists. It is two problems, and they are not the same size:

1. **Sending** — get an HTML email to a list of addresses.
2. **Signup** — collect those addresses in the first place, with confirmation,
   and let people leave.

Almost every write-up of this subject treats these as one thing. They are not,
and for this project the second is the hard one.

## The constraint that decides it

This project has no server. Not "a small server" — none. GitHub Actions runs
the pipeline, GitHub Pages serves the result, and total spend to date is about
seventeen cents, all of it model tokens. Nothing needs patching, nothing has a
backup policy, and nothing can rot while unattended for six months.

That is not incidental. It is why the project survives inattention, which for a
one-person side project is the failure mode that actually kills things.

So the recommended stack in the guide — Ghost or Listmonk on Docker Compose on
a Linux VPS — is a real architecture, competently described, and it is the one
change that would undo the property above. A $5/month VPS is not expensive; a
VPS that must be patched, whose Postgres must be backed up, and whose TLS
certificate must not lapse is a different kind of cost, paid in attention.

Ghost is also a poor fit on its own terms. It is a CMS: it wants to own
authoring and rendering, both of which this project already does from typed
data with attribution invariants enforced in code. Adopting it would mean
either abandoning `render.py` or fighting Ghost's editor forever.

Mautic is not a close call. Lead scoring and drip funnels for a weekly reef
digest is answering a question nobody asked.

## Sending needs no server

At this volume, and only at this volume, the sending half is nearly free and
genuinely serverless:

- **Amazon SES** relays at $0.10 per 1,000 messages. A hundred subscribers,
  weekly, is 5,200 messages a year — about **fifty cents a year**. The existing
  Actions workflow can call it directly after `write_issue`, so sending becomes
  one more pipeline stage rather than a system.
- **SPF, DKIM and DMARC** are DNS records on a domain we already own. Free, and
  required regardless of who does the sending. Send from a subdomain
  (`mail.theloneaquarist.com`) so newsletter reputation can never contaminate
  ordinary mail from the apex.
- **Unsubscribe** is the part that looks like it needs a server and does not.
  RFC 8058 one-click unsubscribe wants an HTTPS endpoint, but Gmail and Yahoo
  require that of *bulk* senders — the 2024 threshold is 5,000 messages a day
  to Gmail, which this project will not approach for years. Below it, a
  `List-Unsubscribe: <mailto:...>` header is a valid opt-out mechanism and
  satisfies CAN-SPAM's requirement for a working one. It is worth building the
  HTTPS form anyway once there is anywhere to host it, but it does not block
  the first send.

The templating advice in the guide holds up and costs nothing. **MJML** compiles
semantic tags into the table-and-inline-CSS HTML that Outlook still requires,
and it runs as a build step — no infrastructure, and it fits the existing
Jinja-to-HTML shape.

## Signup is the actual blocker

A static site cannot accept a form post. That is the whole difficulty, and it
is what a hosted newsletter service is really selling — not the sending, which
is a tenth of a cent, but the subscribe page, the double opt-in mail, the
confirmation state machine, the bounce handling, and the unsubscribe endpoint.

There is also a hard constraint that rules out the obvious shortcut: **the repo
is public**, which is what makes Actions and Pages free. Subscriber addresses
can never be committed to it. A list in an Actions secret would work
mechanically — the 48KB limit holds thousands of addresses — but a secret is
not a database: it has no append operation, so every signup would be a manual
edit. That is fine for a list of one. It does not survive a list of thirty.

## The call

**Stage it, and let the list decide when to spend.**

- **Now — RSS, and send only to myself.** The pipeline already has everything
  needed to render an email; SES plus a one-address list proves deliverability,
  the DNS records, and the template across clients. Cost: fifty cents a year and
  no server. This is also the honest place to sit for the two weeks it takes to
  decide whether the issue is worth anyone else's inbox.
- **When there are real subscribers — a hosted list.** Buttondown is free under
  100 subscribers and has an API the pipeline can post to; beehiiv is the other
  candidate and was already raised. Neither is open source, and that is the
  trade: they solve signup, which is the part that needs a server, in exchange
  for the list living somewhere else. Both export, so it is reversible.
- **If the list ever outgrows a free tier — Listmonk, not Ghost.** Of the three
  options in the guide it is the only one shaped like what this project needs: a
  list manager and nothing more, no CMS, no opinion about how content is made.
  By then a VPS would be buying something real.

The through-line: pay for the part that genuinely needs a server, keep
generating and rendering here, and do not buy a CMS for a pipeline that already
renders itself.
