"""The issue, delivered to inboxes.

Sending is the easy third of delivery. The hard part was always signup: a
static site on GitHub Pages cannot accept a form POST, and this repo is public
so subscriber addresses can never live in it. Buttondown solves exactly that —
it hosts the subscribe page, the double opt-in, the unsubscribe endpoint and
the bounce handling, which is the part that genuinely needs a server. What
arrives here is one POST.

Deliberately thin. Everything above this module — ingest, scoring, picks,
rendering — is provider-agnostic, and the whole Buttondown-specific surface is
the handful of constants below. If the service ever stops fitting, this file is
what gets rewritten, and nothing else does.

## About the constants

The container this was written in cannot reach `docs.buttondown.com` — the
egress proxy blocks it — so the endpoint, the auth header and the field names
below were **not verified against the documentation**. Writing them from memory
and calling them checked is exactly the failure this project has a rule about.

Two things make that safe rather than merely admitted:

* `preview()` renders the exact request — method, URL, headers with the key
  redacted, body — without sending, so it can be read against the real docs.
* `check()` performs a *read* to prove the key and the header shape work. The
  first real interaction with the service is a read, never a send.

If something below is wrong, it is one line in one place.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from .models import Issue

log = logging.getLogger(__name__)

# --- The unverified surface. Check these against the docs before the first send.
API_ROOT = "https://api.buttondown.email/v1"
EMAILS_ENDPOINT = f"{API_ROOT}/emails"
SUBSCRIBERS_ENDPOINT = f"{API_ROOT}/subscribers"
AUTH_HEADER = "Authorization"
AUTH_PREFIX = "Token"
FIELD_SUBJECT = "subject"
FIELD_BODY = "body"
FIELD_STATUS = "status"
STATUS_SEND = "about_to_send"
STATUS_DRAFT = "draft"

# Buttondown's own interlock. The first API send against a key is refused with
# HTTP 400 `sending_requires_confirmation` unless this header is present:
#
#   "Creating an email with status 'about_to_send' requires the
#    X-Buttondown-Live-Dangerously header. This is only required once per
#    API key."
#
# It exists so nobody mails a list by accident while exploring the API, which
# is a good instinct on their part. We satisfy it deliberately rather than
# working around it: the send here is already gated behind a publishing run, a
# non-empty issue, and an explicit workflow input that is off by default.
CONFIRM_HEADER = "X-Buttondown-Live-Dangerously"
# --- End of the unverified surface.

ENV_KEY = "BUTTONDOWN_API_KEY"
USERNAME = "the_lone_aquarist"
SUBSCRIBE_URL = f"https://buttondown.com/{USERNAME}"

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class DeliveryError(RuntimeError):
    """The send did not happen. Always raised, never logged and swallowed.

    A failed send that logs a warning and returns is a week nobody receives,
    discovered — if at all — by someone eventually noticing the silence. The
    run should go red.
    """


def api_key() -> str:
    """The key, from the environment only.

    Never `sources.toml`, never the database, never a log line, and never the
    repository — which is public, and where a committed key would be a leaked
    credential rather than a configuration mistake.
    """
    key = os.environ.get(ENV_KEY, "").strip()
    if not key:
        raise DeliveryError(
            f"{ENV_KEY} is not set. Add it under Settings -> Secrets and "
            "variables -> Actions -> New repository secret."
        )
    return key


def _headers(key: str, *, confirm: bool = False) -> dict[str, str]:
    headers = {
        AUTH_HEADER: f"{AUTH_PREFIX} {key}",
        "Content-Type": "application/json",
    }
    if confirm:
        headers[CONFIRM_HEADER] = "true"
    return headers


def payload(issue: Issue, html: str, *, draft: bool = False) -> dict[str, Any]:
    """The request body. Separated so it can be inspected without sending."""
    from . import render

    return {
        FIELD_SUBJECT: render.subject(issue),
        FIELD_BODY: html,
        FIELD_STATUS: STATUS_DRAFT if draft else STATUS_SEND,
    }


def preview(issue: Issue, html: str, *, draft: bool = False) -> str:
    """The exact request, rendered for reading. Sends nothing, needs no key.

    The body is summarised rather than dumped: the point is to check the shape
    of the request against the documentation, and 40KB of table markup in a
    terminal defeats that.
    """
    body = payload(issue, html, draft=draft)
    shown = dict(body)
    shown[FIELD_BODY] = f"<{len(html)} characters of HTML>"
    lines = [
        f"POST {EMAILS_ENDPOINT}",
        f"{AUTH_HEADER}: {AUTH_PREFIX} ****",
        "Content-Type: application/json",
        *([] if draft else [f"{CONFIRM_HEADER}: true"]),
        "",
        json.dumps(shown, indent=2),
    ]
    return "\n".join(lines)


def check(*, client: httpx.Client | None = None) -> str:
    """Prove the key works, by reading. Sends nothing.

    Mirrors `mailcheck`, which verifies the IMAP mailbox and publishes nothing.
    The first thing this project does against a new service should never be the
    irreversible thing.
    """
    key = api_key()
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        resp = client.get(SUBSCRIBERS_ENDPOINT, headers=_headers(key))
    except httpx.HTTPError as exc:
        raise DeliveryError(f"could not reach the API: {exc}") from exc
    finally:
        if owned:
            client.close()

    if resp.status_code == 401 or resp.status_code == 403:
        raise DeliveryError(
            f"the API rejected the key (HTTP {resp.status_code}). Check the "
            f"secret, and check that the auth header is '{AUTH_PREFIX} <key>'."
        )
    if resp.status_code >= 400:
        raise DeliveryError(f"unexpected response HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        count = data.get("count", len(data.get("results", [])))
    except (json.JSONDecodeError, AttributeError):
        return f"HTTP {resp.status_code}, but the body was not the JSON expected: {resp.text[:200]}"
    return f"key works. {count} subscriber(s) on the list."


def send(
    issue: Issue, html: str, *, draft: bool = False, client: httpx.Client | None = None
) -> str:
    """Send the issue. Returns a short description of what happened.

    Refuses an empty issue. A week with nothing in it is a week not to write —
    an email whose body is a masthead and a footer spends subscriber goodwill
    to say nothing, and goodwill is the only currency a newsletter has.
    """
    if not issue.items:
        raise DeliveryError("refusing to send an issue with no items")

    key = api_key()
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        resp = client.post(
            EMAILS_ENDPOINT,
            # The confirmation header only on a real send. A draft creates
            # nothing that reaches anyone, so it has no interlock to satisfy.
            headers=_headers(key, confirm=not draft),
            json=payload(issue, html, draft=draft),
        )
    except httpx.HTTPError as exc:
        raise DeliveryError(f"the send failed: {exc}") from exc
    finally:
        if owned:
            client.close()

    if resp.status_code >= 400:
        raise DeliveryError(f"the send was refused, HTTP {resp.status_code}: {resp.text[:500]}")

    what = "drafted" if draft else "sent"
    log.info("%s issue for %s", what, f"{issue.date:%Y-%m-%d}")
    return f"{what}: {len(issue.items)} items, HTTP {resp.status_code}"
