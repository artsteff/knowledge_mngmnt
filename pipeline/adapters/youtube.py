"""YouTube source adapter. Ports the working parts of scripts/youtube/youtube_monitor.py."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..core.transcribe import fetch_youtube_autosubs, transcribe_audio_via_openai
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
        ]
        if source.get("private"):
            cmd += ["--cookies-from-browser", "chrome"]
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
                    log.warning("No transcript for %s (%s)", vid, title[:60])
                    errors.append(f"transcript-missing:{vid}")
                    seen.add(vid)   # don't retry indefinitely
                    continue

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
