"""The Resource section's video still.

YouTube publishes every video's thumbnail at a stable path keyed by the video
id, and `normalize.py` already puts that id on the item. So the picture costs
no API call and no quota — the URL is derived, not discovered.

The image is **fetched at build time and committed**, not hotlinked. Three
reasons, in the order they matter:

1. A hotlinked image means every reader's browser makes a request to Google
   just by opening the page. A digest that links out is one thing; one that
   silently reports its readers to a third party is another.
2. `daily-dive preview` is offline and deterministic, and it should render the
   real page rather than a page with a hole in it.
3. Mail clients strip or proxy remote images. When the email milestone lands,
   a file we already have is the only version that works.

The cost is a few tens of kilobytes a week in the repo, which is the cheap side
of that trade.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

import httpx

from . import brand

log = logging.getLogger(__name__)

DIR = "assets/thumbs"

# maxresdefault is 1280x720 and does not exist for every video — it is only
# generated when the uploader supplied art that large. hqdefault always exists,
# but it is 4:3 with letterbox bars baked in, so it is the fallback rather than
# the default. sddefault sits between them and is likewise not guaranteed.
CANDIDATES = ("maxresdefault", "hqdefault")

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
USER_AGENT = f"{brand.BOT_NAME}/{brand.BOT_VERSION} (+{brand.SITE_URL}; {brand.CONTACT_EMAIL})"

# A real thumbnail is tens of kilobytes. YouTube's placeholder for a missing
# size is a 120x90 grey rectangle that weighs about 1 KB, and it comes back with
# a 200, so "did we get bytes?" is not a test. Neither is "did we get a JPEG?".
MIN_BYTES = 4_000
MIN_WIDTH = 320

# Start-of-frame markers carry the dimensions. DHT/DQT/SOS and the DNL/RSTn
# range are skipped explicitly because they are not SOF frames despite sitting
# in the same 0xC0-0xCF block.
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def url_for(video_id: str, size: str = "maxresdefault") -> str:
    return f"https://i.ytimg.com/vi/{video_id}/{size}.jpg"


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Pixel dimensions from JPEG headers, or None if this isn't readable JPEG.

    The same job `render._png_size` does for the masthead, and for the same
    reason: the template wants width and height so the browser can reserve the
    box before the image arrives, and a page that reflows as its picture loads
    is a page that moves under your thumb while you're reading it.
    """
    if not data.startswith(b"\xff\xd8\xff"):
        return None
    i = 2
    end = len(data)
    while i + 3 < end:
        if data[i] != 0xFF:
            # Not sitting on a marker — the file is truncated or not JPEG after
            # all. Bail rather than scanning for something that looks like one.
            return None
        marker = data[i + 1]
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2  # standalone markers carry no length
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if length < 2:
            return None
        if marker in _SOF_MARKERS:
            if i + 9 > end:
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return (width, height)
        i += 2 + length
    return None


def _acceptable(data: bytes) -> tuple[int, int] | None:
    """Dimensions if this is a usable thumbnail, else None (with a reason logged)."""
    if len(data) < MIN_BYTES:
        log.info("thumbnail rejected: %d bytes, likely a placeholder", len(data))
        return None
    size = jpeg_size(data)
    if size is None:
        log.info("thumbnail rejected: not a readable JPEG (%d bytes)", len(data))
        return None
    if size[0] < MIN_WIDTH:
        log.info("thumbnail rejected: %dx%d is too small to use", *size)
        return None
    return size


def fetch(
    video_id: str, out_dir: Path, *, client: httpx.Client | None = None
) -> tuple[str, int, int] | None:
    """Fetch and store the thumbnail. Returns (relative path, width, height).

    Returns None on any failure, having logged it. This is deliberately
    non-fatal: the Resource section renders text-only without a picture, and an
    unreachable CDN must never be the reason an issue does not go out.

    Old thumbnails are never removed. Dated permalinks reference theirs
    permanently, so a cleanup pass would quietly blank out every back issue.
    """
    if not video_id or "/" in video_id or "." in video_id:
        # The id reaches here from a feed, so it is not ours. It becomes a
        # filename and a URL path segment; anything that could climb out of
        # either is refused rather than sanitised.
        log.warning("refusing implausible video id %r", video_id)
        return None

    owned = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    try:
        for size in CANDIDATES:
            try:
                resp = client.get(url_for(video_id, size))
            except httpx.HTTPError as exc:
                log.warning("thumbnail fetch failed for %s/%s: %s", video_id, size, exc)
                continue
            if resp.status_code != 200:
                log.info("no %s for %s (HTTP %d)", size, video_id, resp.status_code)
                continue
            dims = _acceptable(resp.content)
            if dims is None:
                continue

            rel = f"{DIR}/{video_id}.jpg"
            path = out_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
            log.info("thumbnail %s %dx%d (%d bytes)", rel, dims[0], dims[1], len(resp.content))
            return (rel, dims[0], dims[1])
    finally:
        if owned:
            client.close()

    log.warning("no usable thumbnail for %s", video_id)
    return None


def existing(video_id: str, out_dir: Path, *, depth: int = 0) -> tuple[str, int, int] | None:
    """An already-committed thumbnail, or None.

    Lets the dated permalink and the offline preview reuse what a publishing run
    fetched, without going near the network. `depth` mirrors
    `render.find_header_image`: how far below the site root the page sits.
    """
    if not video_id:
        return None
    path = out_dir / DIR / f"{video_id}.jpg"
    if not path.is_file():
        return None
    dims = jpeg_size(path.read_bytes())
    if dims is None:
        log.warning("committed thumbnail %s is unreadable", path)
        return None
    return (("../" * depth) + f"{DIR}/{video_id}.jpg", dims[0], dims[1])
