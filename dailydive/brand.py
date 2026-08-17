"""Who this is and how to reach them.

Single source of truth. The contact address in particular appears in the
crawler's User-Agent, the about page, and the removal policy — three places
that must never disagree, because a publisher who wants to be delisted should
find the same address wherever they look.
"""

from __future__ import annotations

# The person writing it. Appears in the morning greeting.
EDITOR = "Isaac"

# Reachable by a publisher who wants something removed, and by anyone whose
# feed the crawler is hitting. Keep it monitored.
CONTACT_EMAIL = "theloneaquarist@gmail.com"

SITE_NAME = "The Lone Aquarist"
PUBLICATION = "Weekly Dive"
SITE_URL = "https://www.theloneaquarist.com"

# How often an issue goes out, as an adjective. Used in the tagline, the page
# description and the masthead's alt text, so changing the cron and changing
# this is one edit — the page can never claim a cadence it does not keep.
CADENCE = "weekly"

# The same cadence as a noun, for prose that needs "each week" rather than
# "a weekly digest". Stated rather than derived: stripping "ly" off CADENCE
# works for "weekly" and turns "daily" into "dai", and a cadence change is
# already a two-line edit here.
CADENCE_NOUN = "week"

# The masthead's second line. Deliberately says nothing about cadence: the
# publication is called Weekly Dive, so "Your weekly news" underneath it is a
# stutter. The tagline's job is scope — what this covers — and the name's job
# is rhythm.
TAGLINE = "Saltwater and reef news for the marine aquarist."

# The page's meta description, which no one reads beside the wordmark, so the
# cadence belongs here where it is useful rather than redundant.
DESCRIPTION = (
    f"A {CADENCE} digest of saltwater and reef aquarium news. "
    "Every item links to its source."
)

BOT_NAME = "DailyDiveBot"
BOT_VERSION = "0.1"

# How the reader is addressed. Warm, not cutesy — they keep a reef tank.
AUDIENCE = "reefing family"
