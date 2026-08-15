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
PUBLICATION = "Daily Dive"
SITE_URL = "https://www.theloneaquarist.com"

# How often an issue goes out, as an adjective. Used in the tagline, the page
# description and the masthead's alt text, so changing the cron and changing
# this is one edit — the page can never claim a cadence it does not keep.
CADENCE = "weekly"

# The masthead's second line and the page's meta description. Derived rather
# than written out, because the whole point of this file is that a rename is a
# single edit here and not a search across templates.
TAGLINE = f"Your {CADENCE} news for the marine aquarist."
DESCRIPTION = (
    f"A {CADENCE} digest of saltwater and reef aquarium news. "
    "Every item links to its source."
)

BOT_NAME = "DailyDiveBot"
BOT_VERSION = "0.1"

# How the reader is addressed. Warm, not cutesy — they keep a reef tank.
AUDIENCE = "reefing family"
