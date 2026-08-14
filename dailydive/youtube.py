"""Telling a video from a Short.

playlistItems.list, which is how videos reach the pipeline, does not say
whether an entry is a Short — there is no `isShort` field anywhere in the Data
API. What it does expose, through a second call, is duration, and duration is
the definition: YouTube classifies an upload as a Short at three minutes or
less. So that is what this asks.

The alternative approaches are worse. Matching "#shorts" in the title catches
only the ones whose author bothered to tag them, and requesting
youtube.com/shorts/<id> to watch for a redirect means an extra unauthenticated
request per video against a path robots.txt disallows.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from .models import Item

log = logging.getLogger(__name__)

VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"

# YouTube's own cutoff. Raising this drops legitimate short clips; lowering it
# lets Shorts through. Three minutes is the line the platform itself draws.
SHORTS_MAX_SECONDS = 180

# videos.list accepts up to 50 ids per request and costs 1 quota unit per
# call regardless, so batching is the whole ballgame: 250 videos is 5 units.
BATCH = 50

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def parse_duration(iso: str) -> int | None:
    """ISO 8601 duration -> seconds. None if it doesn't parse.

    The API returns durations like PT4M13S, and PT0S for a live stream that
    hasn't started. Unparseable input returns None so the caller can keep the
    video rather than guess at its length.
    """
    match = _DURATION_RE.match(iso or "")
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def fetch_durations(video_ids: list[str], *, client: httpx.Client, api_key: str) -> dict[str, int]:
    """video id -> length in seconds, for as many as could be looked up."""
    durations: dict[str, int] = {}
    for start in range(0, len(video_ids), BATCH):
        batch = video_ids[start : start + BATCH]
        try:
            resp = client.get(
                VIDEOS_ENDPOINT,
                params={"part": "contentDetails", "id": ",".join(batch), "key": api_key},
            )
            resp.raise_for_status()
            payload = json.loads(resp.content)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            log.warning("could not look up durations for %d video(s): %s", len(batch), exc)
            continue

        if error := payload.get("error"):
            log.error("YouTube API error %s: %s", error.get("code"), error.get("message"))
            continue

        for entry in payload.get("items", []):
            iso = (entry.get("contentDetails") or {}).get("duration")
            seconds = parse_duration(iso) if iso else None
            if seconds is not None:
                durations[entry["id"]] = seconds
    return durations


def drop_shorts(
    items: list[Item],
    *,
    client: httpx.Client,
    api_key: str,
    max_seconds: int = SHORTS_MAX_SECONDS,
) -> list[Item]:
    """Remove Shorts, and record the duration of everything that stays.

    A video whose length could not be determined is KEPT. The failure modes
    are not symmetric: dropping a real video because a lookup failed loses
    reporting silently, while keeping one Short is a visible blemish someone
    can point at.
    """
    video_ids = [i.extra["video_id"] for i in items if i.extra.get("video_id")]
    if not video_ids:
        return items

    durations = fetch_durations(video_ids, client=client, api_key=api_key)
    if not durations:
        log.warning("no durations resolved — keeping every video rather than guessing")
        return items

    kept: list[Item] = []
    dropped = 0
    for item in items:
        vid = item.extra.get("video_id")
        if vid is None:
            kept.append(item)
            continue

        seconds = durations.get(vid)
        if seconds is None:
            kept.append(item)
            continue
        if seconds <= max_seconds:
            dropped += 1
            continue

        # Kept on the item so the scoring pass and the page can both see it —
        # "18 min" is genuinely useful to a reader deciding what to watch.
        kept.append(item.model_copy(update={"extra": {**item.extra, "duration_s": str(seconds)}}))

    if dropped:
        log.info("dropped %d Short(s) (<= %ds)", dropped, max_seconds)
    return kept
