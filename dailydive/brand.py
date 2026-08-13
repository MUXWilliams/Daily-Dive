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

BOT_NAME = "DailyDiveBot"
BOT_VERSION = "0.1"

# How the reader is addressed. Warm, not cutesy — they keep a reef tank.
AUDIENCE = "reefing family"
