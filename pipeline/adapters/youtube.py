"""YouTube source adapter. Ports the working parts of scripts/youtube/youtube_monitor.py."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core.transcribe import (
    cookies_args, fetch_transcript_via_api,
    fetch_youtube_autosubs,
    transcribe_audio_via_local_whisper, transcribe_audio_via_openai,
)
from ._base import FetchResult, NormalizedItem, SourceAdapter

log = logging.getLogger(__name__)

YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")
SHORTS_MAX_SECONDS = 180
DEFAULT_FETCH_LIMIT = 50
# Sleep between processing each video to avoid YouTube IP-level rate limiting.
# Each video makes multiple yt-dlp + transcript-api HTTP calls; without spacing,
# 80+ videos in a single run trips bot detection.
INTER_VIDEO_SLEEP = float(os.environ.get("KM_VIDEO_SLEEP_SECONDS", "5"))
# Circuit breaker: abort the run after this many consecutive transcript failures.
# A streak of failures is the signature of an IP-level rate limit; continuing
# would burn through 3-strike retry counters and lose videos permanently.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("KM_CIRCUIT_BREAKER_THRESHOLD", "5"))


class YouTubeAdapter(SourceAdapter):
    name = "youtube"

    def __init__(self, sources_file: Path):
        self.sources_file = sources_file

    def _load_sources(self) -> list[dict]:
        return json.loads(self.sources_file.read_text())["sources"]

    def _fetch_source_videos(self, source: dict) -> list[dict]:
        fetch_limit = source.get("fetch_limit", DEFAULT_FETCH_LIMIT)
        cmd = [
            YTDLP_BIN, "--flat-playlist", "--dump-json", "--no-warnings",
            "--playlist-end", str(fetch_limit),
            *cookies_args(source.get("private", False)),
        ]
        cmd.append(source["url"])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            videos = []
            for line in r.stdout.strip().splitlines():
                if line.strip():
                    try:
                        videos.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return videos
        except subprocess.TimeoutExpired:
            log.warning("Timeout fetching %s", source["name"])
            return []
        except Exception as e:
            log.warning("Error fetching %s: %s", source["name"], e)
            return []

    def fetch(self, state: dict) -> FetchResult:
        seen = set(state.get("seen", []))
        items: list[NormalizedItem] = []
        errors: list[str] = []
        # Pending failure increments — only committed if the run completes without
        # the circuit breaker tripping. If we abort due to rate limiting, these
        # videos retry fresh next cron (no penalty).
        pending_failed: list[tuple[str, str]] = []  # (vid, title)
        consecutive_failures = 0
        breaker_tripped = False

        for source in self._load_sources():
            if breaker_tripped:
                break
            log.info("YT source: %s", source["name"])
            for v in self._fetch_source_videos(source):
                vid = v.get("id")
                if not vid or vid in seen:
                    continue
                duration = v.get("duration") or 0
                if duration and duration <= SHORTS_MAX_SECONDS:
                    seen.add(vid)
                    continue

                # Rate-limit guard: space out per-video processing.
                if INTER_VIDEO_SLEEP > 0:
                    time.sleep(INTER_VIDEO_SLEEP)

                title = v.get("title", "Untitled")
                channel = v.get("channel") or v.get("uploader") or source["name"]
                url = f"https://youtu.be/{vid}"

                transcript = fetch_transcript_via_api(vid)
                if not transcript:
                    log.info("youtube-transcript-api missed %s; trying yt-dlp autosubs", vid)
                    transcript = fetch_youtube_autosubs(vid, private=source.get("private", False))
                if not transcript:
                    log.info("No autosubs for %s; trying local Whisper", vid)
                    transcript = transcribe_audio_via_local_whisper(vid, private=source.get("private", False))
                if not transcript:
                    log.info("Local Whisper missed %s; trying OpenAI Whisper API", vid)
                    transcript = transcribe_audio_via_openai(vid, private=source.get("private", False))

                if not transcript:
                    consecutive_failures += 1
                    pending_failed.append((vid, title))
                    errors.append(f"transcript-missing:{vid}")
                    log.warning("No transcript for %s (consecutive=%d): %s",
                                vid, consecutive_failures, title[:60])
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        log.error(
                            "🚨 Circuit breaker tripped after %d consecutive failures — "
                            "aborting run. %d pending failures will NOT be penalized "
                            "(retry counters untouched). Likely YouTube IP rate limit. "
                            "Next cron will retry fresh.",
                            consecutive_failures, len(pending_failed),
                        )
                        breaker_tripped = True
                        break
                    continue

                # Success — clear any prior retry counter and reset streak.
                consecutive_failures = 0
                if "failed" in state and vid in state["failed"]:
                    del state["failed"][vid]

                items.append(NormalizedItem(
                    source_id=vid,
                    url=url,
                    title=title,
                    author=channel,
                    date=datetime.now(timezone.utc).date().isoformat(),
                    raw_text=transcript[:10000],
                    source_type="video",
                    source_meta={
                        "playlist_source": source["name"],
                        "duration_s": duration,
                    },
                ))
                seen.add(vid)

        # Commit pending failures ONLY if the breaker didn't trip.
        if not breaker_tripped:
            fails = state.setdefault("failed", {})
            for vid, title in pending_failed:
                fails[vid] = fails.get(vid, 0) + 1
                if fails[vid] >= 3:
                    log.warning("Giving up on %s after %d attempts (%s)", vid, fails[vid], title[:60])
                    seen.add(vid)
                    del fails[vid]

        state["seen"] = sorted(seen)
        if breaker_tripped:
            errors.append("circuit-breaker-tripped")
        return FetchResult(new_items=items, errors=errors)
