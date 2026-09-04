"""Make the suite's paths mean what they say.

Most of these tests read repository files by relative path — `sources.toml`,
`.github/workflows/daily.yml`, `CLAUDE.md`, `site/about.html` — because the
things being asserted *are* repository files: that the workflow's cron matches
the recency window, that the about page carries the contact address, that no
template claims a cadence the schedule does not keep.

That only works from the repository root. Several of those reads happen at
module scope, so running pytest from anywhere else did not fail a test — it
failed *collection*, with a FileNotFoundError before a single test ran. CI has
always run from the root, so it never bit; an IDE runner or a `cd tests`
would have.

conftest is imported before the test modules it sits beside, which is why the
chdir belongs here and not in a fixture. A fixture runs too late: by then the
module-level reads have already happened.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.chdir(REPO)
