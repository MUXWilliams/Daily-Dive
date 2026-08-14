"""Newsletters in, Items out.

Some outlets that block crawlers still send email. Pet Age, Pet Product News
and Pet Business all refuse an HTTP client and all run newsletters, so the
mailbox is the door they left open.

Three properties matter more than the parsing:

*Read-only.* The mailbox is opened readonly and nothing is ever flagged or
deleted. A run that dies halfway cannot lose mail, and the archive's existing
URL dedupe already stops a story appearing twice — so idempotence comes from
the same mechanism everything else uses, not from mutating someone's inbox.

*Allowlisted.* An inbox is an untrusted input: anyone who learns the address
can put content into a pipeline that publishes to a public page. Only senders
named in the source's `senders` list are read, and an empty list refuses
everything.

*Linked.* A newsletter item is only publishable if it yields a public URL to
send the reader to. No link, no item — which is also what keeps this on the
right side of a subscription's terms, since a summary with nowhere to click is
republication rather than a digest entry.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit

import httpx

from .models import AttributionError, Item, Source

log = logging.getLogger(__name__)

IMAP_PORT = 993

# Anchor text shorter than this is navigation, not a headline.
MIN_HEADLINE_CHARS = 25
MAX_HEADLINE_CHARS = 180

# Anchor text that is never a story, however long it runs.
_CHROME = (
    "unsubscribe", "view in browser", "view this email", "privacy policy",
    "manage preferences", "update your preferences", "advertise", "contact us",
    "subscribe", "forward to a friend", "add us to your address book",
    "terms of service", "read more", "click here", "learn more", "sign up",
    "follow us", "share this", "download the app", "media kit",
)

# Hosts that exist only to count clicks. A link through one of these has to be
# resolved before publication: publishing the wrapper would send every reader
# through someone else's analytics, and would rot the moment the campaign ends.
_TRACKERS = (
    "list-manage.com", "sendgrid.net", "mailchimp.com", "cmail1.com",
    "cmail2.com", "hubspotlinks.com", "constantcontact.com", "mailgun.org",
    "sparkpostmail.com", "exacttarget.com", "salesforce-communications.com",
    "campaign-archive.com", "rs6.net", "bit.ly", "trk.klclick.com",
)

# Query params that carry the real destination inside a wrapper URL.
_DESTINATION_PARAMS = ("url", "u", "target", "redirect", "redirect_url", "link", "dest", "r")

# Vocabulary gate for general pet-trade newsletters, which are mostly dogs,
# cats and retail. This is a cost control, not the real filter — the scoring
# pass is the accurate judge. Kept generous on purpose: dropping a relevant
# story silently is worse than paying to score an irrelevant one, so anything
# plausibly marine passes and every drop is logged.
MARINE_TERMS = frozenset(
    """
    marine saltwater reef reefs coral corals aquaria aquarium aquariums
    aquatic aquatics aquaculture fish fishes livestock invertebrate
    invertebrates anemone clownfish tang wrasse angelfish frag fragging
    zoanthid acropora montipora cichlid seahorse ornamental mariculture
    quarantine salinity skimmer sump refugium cites bleaching
    """.split()
)


class _LinkHarvester(HTMLParser):
    """Every anchor in the email, as (href, text).

    A newsletter is fifteen stories in one message, so the unit of ingestion
    has to be the link rather than the email — otherwise one dog-food story
    drags a coral story down with it, or the reverse.
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if href.startswith(("http://", "https://")):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            self.links.append((self._href, text))
            self._href, self._text = None, []


def _decode(raw: str | None) -> str:
    """MIME-encoded header -> readable text."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw.strip()


def _html_body(message: Message) -> str:
    """The richest body part available, preferring HTML for its links."""
    parts: list[str] = []
    plain: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (parts if part.get_content_subtype() == "html" else plain).append(text)
    return "\n".join(parts or plain)


def is_tracker(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == t or host.endswith("." + t) for t in _TRACKERS)


def unwrap(url: str, *, client: httpx.Client | None = None) -> str | None:
    """The real destination behind a click-tracking link.

    Two shapes. Most wrappers carry the destination in a query parameter, which
    costs nothing to read. The rest — Mailchimp's especially — use an opaque
    campaign id, and the only way to learn the target is to follow the
    redirect. Returns None when it cannot be resolved, because publishing an
    unresolved tracker is worse than dropping the item.
    """
    if not is_tracker(url):
        return url

    for key, value in parse_qsl(urlsplit(url).query):
        if key.lower() in _DESTINATION_PARAMS and value.startswith(("http://", "https://")):
            return value

    if client is None:
        return None
    try:
        resp = client.head(url, follow_redirects=True)
        final = str(resp.url)
    except httpx.HTTPError as exc:
        log.warning("could not resolve tracking link (%s)", exc)
        return None
    return None if is_tracker(final) else final


def looks_marine(text: str) -> bool:
    """True if the text uses any marine vocabulary at all."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    return bool(words & MARINE_TERMS)


def items_from_message(
    source: Source,
    message: Message,
    *,
    client: httpx.Client | None = None,
    require_marine: bool = True,
) -> list[Item]:
    """One newsletter -> the stories it links to."""
    sender = parseaddr(message.get("From", ""))[1].lower()
    allowed = {s.lower() for s in source.senders}
    if sender not in allowed:
        log.warning("%s: ignoring mail from %r, not in the allowlist", source.id, sender)
        return []

    try:
        published = parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError):
        published = None
    if published is None:
        log.warning("%s: message has no usable Date, dropped", source.id)
        return []
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)

    harvester = _LinkHarvester()
    harvester.feed(_html_body(message))

    items: list[Item] = []
    seen: set[str] = set()
    for href, text in harvester.links:
        lowered = text.lower()
        if not (MIN_HEADLINE_CHARS <= len(text) <= MAX_HEADLINE_CHARS):
            continue
        if any(marker in lowered for marker in _CHROME):
            continue
        if require_marine and not looks_marine(text):
            log.debug("%s: gated out %r", source.id, text[:60])
            continue

        target = unwrap(href, client=client)
        if target is None:
            log.info("%s: dropped %r — could not resolve its tracking link", source.id, text[:60])
            continue
        if target in seen:
            continue
        seen.add(target)

        try:
            items.append(
                Item(
                    source_id=source.id,
                    source_name=source.display_name,
                    title=text,
                    url=target,
                    published_at=published,
                    raw_text=_decode(message.get("Subject")) or None,
                    category_hint=source.category_hint,
                    extra={"newsletter": _decode(message.get("Subject"))[:120]},
                )
            )
        except (AttributionError, ValueError) as exc:
            log.warning("%s: %r dropped (%s)", source.id, text[:60], exc)

    return items


def fetch(
    source: Source,
    *,
    user: str,
    password: str,
    days: int,
    client: httpx.Client | None = None,
) -> list[Item]:
    """Read recent newsletters from the mailbox named by the source URL.

    The URL is `imap://host/mailbox` — the host to connect to and the label to
    read. Credentials never appear in it.
    """
    parts = urlsplit(source.url)
    host = parts.netloc or "imap.gmail.com"
    folder = parts.path.strip("/") or "INBOX"
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%d-%b-%Y")

    items: list[Item] = []
    with imaplib.IMAP4_SSL(host, IMAP_PORT) as imap:
        imap.login(user, password)
        # readonly: nothing is flagged, moved or deleted. A crashed run cannot
        # lose mail, and repeats are handled by the archive like everywhere else.
        status, _ = imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            log.error("%s: no mailbox named %r", source.id, folder)
            return []

        # Narrow server-side when the source speaks for exactly one outlet,
        # which is the normal case: each publisher is its own source so each
        # item can be credited to the outlet that actually wrote it. Filtering
        # here means one source never even downloads another's mail.
        criteria = ["SINCE", since]
        if len(source.senders) == 1:
            criteria += ["FROM", f'"{source.senders[0]}"']
        status, data = imap.search(None, *criteria)
        if status != "OK":
            log.error("%s: IMAP search failed", source.id)
            return []

        ids = data[0].split()
        log.info("%s: %d message(s) since %s", source.id, len(ids), since)
        for message_id in ids:
            status, payload = imap.fetch(message_id, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            message = email.message_from_bytes(payload[0][1])
            items.extend(items_from_message(source, message, client=client))

    return items


# The Actions log of a public repo is public. A wrong label would otherwise
# dump a personal inbox into it, so the check refuses to read INBOX outright
# rather than trusting the config to be right.
FORBIDDEN_MAILBOXES = frozenset({"inbox", "[gmail]/all mail", "[gmail]/important"})


def describe(source: Source, *, user: str, password: str, days: int) -> str:
    """What is actually in the mailbox, without building or publishing anything.

    Exists to answer two questions at once: do the credentials work, and what
    are the real From addresses to put in the allowlist? Those addresses cannot
    be guessed — publications send from vendor subdomains, not their own domain
    — so they have to be read off real mail.
    """
    parts = urlsplit(source.url)
    host = parts.netloc or "imap.gmail.com"
    folder = parts.path.strip("/")

    if not folder or folder.lower() in FORBIDDEN_MAILBOXES:
        return (
            f"refusing to read {folder or 'INBOX'!r}: this prints to a public log, so the "
            "check only reads a dedicated label. Set one in the source URL."
        )

    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%d-%b-%Y")
    lines = [f"connecting to {host} as {user} …"]

    with imaplib.IMAP4_SSL(host, IMAP_PORT) as imap:
        imap.login(user, password)
        lines.append("login OK")

        status, _ = imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            lines.append(f"no mailbox named {folder!r} — check the Gmail label name and filter")
            return "\n".join(lines)
        lines.append(f"mailbox {folder!r} opened read-only")

        status, data = imap.search(None, "SINCE", since)
        ids = data[0].split() if status == "OK" else []
        lines.append(f"{len(ids)} message(s) since {since}")
        if not ids:
            lines.append("nothing to read yet — subscribe, then re-run once mail arrives")
            return "\n".join(lines)

        allowed = {s.lower() for s in source.senders}
        seen: dict[str, int] = {}
        rows: list[str] = []
        for message_id in ids[-40:]:
            status, payload = imap.fetch(message_id, "(BODY.PEEK[HEADER])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            header = email.message_from_bytes(payload[0][1])
            sender = parseaddr(header.get("From", ""))[1].lower()
            seen[sender] = seen.get(sender, 0) + 1
            rows.append(f"    {sender:44} {_decode(header.get('Subject'))[:60]}")

        lines += ["", "senders found:"]
        for sender, count in sorted(seen.items(), key=lambda kv: -kv[1]):
            mark = "allowed" if sender in allowed else "NOT in allowlist"
            lines.append(f"  {sender:44} {count:>3} message(s)  [{mark}]")
        lines += ["", "recent subjects:", *rows[-15:]]
        lines += [
            "",
            "Add the senders you actually subscribed to into the source's `senders`",
            "list in sources.toml, then enable the source. Anything not listed is",
            "refused, which is the point.",
        ]
    return "\n".join(lines)
