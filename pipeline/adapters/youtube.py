"""YouTube source adapter. Ports the working parts of scripts/youtube/youtube_monitor.py."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..core.transcribe import cookies_args, fetch_youtube_autosubs, transcribe_audio_via_openai
from ._base import FetchResult, NormalizedItem, SourceAdapter

log = logging.getLogger(__name__)

YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")
SHORTS_MAX_SECONDS = 180
PLAYLIST_END = 50


class YouTubeAdapter(SourceAdapter):
    name = "youtube"

    def __init__(self, sources_file: Path):
        self.sources_file = sources_file

    def _load_sources(self) -> list[dict]:
        return json.loads(self.sources_file.read_text())["sources"]

    def _fetch_source_videos(self, source: dict) -> list[dict]:
        cmd = [
            YTDLP_BIN, "--flat-playlist", "--dump-json", "--no-warnings",
            "--playlist-end", str(PLAYLIST_END),
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

        for source in self._load_sources():
            log.info("YT source: %s", source["name"])
            for v in self._fetch_source_videos(source):
                vid = v.get("id")
                if not vid or vid in seen:
                    continue
                duration = v.get("duration") or 0
                if duration and duration <= SHORTS_MAX_SECONDS:
                    seen.add(vid)
                    continue

                title = v.get("title", "Untitled")
                channel = v.get("channel") or v.get("uploader") or source["name"]
                url = f"https://youtu.be/{vid}"

                transcript = fetch_youtube_autosubs(vid, private=source.get("private", False))
                if not transcript:
                    log.info("No autosubs for %s; trying Whisper", vid)
                    transcript = transcribe_audio_via_openai(vid, private=source.get("private", False))
                if not transcript:
                    # Don't mark seen on failure — let the next cron retry.
                    # Cap retries via state['failed'] counter; after MAX_FAIL attempts, give up.
                    fails = state.setdefault("failed", {})
                    fails[vid] = fails.get(vid, 0) + 1
                    if fails[vid] >= 3:
                        log.warning("Giving up on %s after %d attempts (%s)", vid, fails[vid], title[:60])
                        seen.add(vid)
                        del fails[vid]
                    else:
                        log.warning("No transcript for %s (attempt %d/3): %s", vid, fails[vid], title[:60])
                    errors.append(f"transcript-missing:{vid}")
                    continue
                # Success — clear any retry counter
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

        state["seen"] = sorted(seen)
        return FetchResult(new_items=items, errors=errors)
