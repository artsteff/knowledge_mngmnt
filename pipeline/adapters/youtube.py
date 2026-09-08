"""YouTube source adapter. Ports the working parts of scripts/youtube/youtube_monitor.py."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core.transcribe import cookies_args
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
# A streak of failures is the signature of an IP-level rate limit, and there is
# no point hammering a blocked endpoint for the rest of the run. Whether those
# failures count against a video's retry budget is decided separately, at the
# end of fetch(), by whether anything succeeded at all.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("KM_CIRCUIT_BREAKER_THRESHOLD", "5"))
# Ceiling on videos looked up per run. This used to cap transcription, which was
# the expensive step; now the run only reads metadata (about 1.6 s per video, no
# model, no money), so it can be much higher. Anything not reached stays unseen
# and is picked up by the next run.
MAX_DISCOVER_PER_RUN = int(os.environ.get("KM_MAX_DISCOVER_PER_RUN", "40"))
# Upper bound on video length. The queue on 2026-09-06 held 119 videos with a
# median of 27 minutes - and a mean of 47, because sixteen multi-hour
# conference recordings (the longest 9h11m) carried 44 of the 93 hours between
# them. A nine-hour livestream does not compress into a summary card, and it
# costs more to transcribe than everything it sits next to. 90 minutes keeps
# full-length podcast episodes.
MAX_VIDEO_SECONDS = int(os.environ.get("KM_MAX_VIDEO_SECONDS", "5400"))


class YouTubeAdapter(SourceAdapter):
    name = "youtube"

    def __init__(self, sources_file: Path):
        self.sources_file = sources_file

    @staticmethod
    def _log_ytdlp_version() -> None:
        """Log which yt-dlp is actually being used, and warn when it is stale.

        This is the failure that cost the pipeline a day. yt-dlp resolves
        formats happily long after YouTube changes its signature scheme, and
        only the *download* starts returning 403 - so `--simulate` looks fine
        while every real fetch fails. On 2026-09-06 the venv held 2026.03.17 and
        Homebrew 2026.06.09; both 403'd, and 2026.08.19 downloaded first try.
        Note the binary comes from PATH, so the version in use is not
        necessarily the one in this venv.
        """
        try:
            r = subprocess.run([YTDLP_BIN, "--version"], capture_output=True,
                               text=True, timeout=20)
            ver = r.stdout.strip()
        except Exception as e:
            log.warning("Could not determine yt-dlp version: %s", e)
            return
        path = shutil.which(YTDLP_BIN) or YTDLP_BIN
        log.info("yt-dlp %s (%s)", ver, path)
        try:
            released = datetime.strptime(ver.split(".dev")[0], "%Y.%m.%d")
            age = (datetime.now() - released).days
            if age > 90:
                log.warning(
                    "yt-dlp is %d days old (%s). YouTube breaks old versions at "
                    "download time, not at format resolution - a 403 on every "
                    "audio fetch is the symptom. Upgrade it.", age, ver,
                )
        except ValueError:
            pass

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

    @staticmethod
    def _is_short(v: dict, min_seconds: int = SHORTS_MAX_SECONDS) -> bool:
        """Shorts are not worth transcribing, and they cannot be detected by
        duration alone.

        A channel URL expands into several tabs, and entries from the Shorts tab
        come back from --flat-playlist with `duration: None`. The old check was
        `if duration and duration <= 180`, which `None` skips entirely - so every
        Short sailed through into transcription. Shorts rarely have captions, so
        they failed, and five of them were enough to trip the circuit breaker on
        every single run (2026-09-06). The URL is the reliable signal.

        `min_seconds` is per-source (`min_duration_s` in the sources file)
        because the threshold is not universal: YouTube Creators publishes
        official one- and two-minute Partner Program announcements that the
        default 180 s would throw away as Shorts. A genuine Short is still
        caught by its URL whatever the threshold.
        """
        url = v.get("url") or v.get("webpage_url") or ""
        if "/shorts/" in url:
            return True
        if (v.get("playlist_title") or "").endswith("- Shorts"):
            return True
        duration = v.get("duration")
        return duration is not None and duration <= min_seconds

    def _list_candidates(self, state: dict, seen: set) -> list[dict]:
        """Phase 1 - list every source before transcribing anything.

        Listing is cheap; transcribing is what gets rate-limited. Doing all the
        listing first means a source that fails to transcribe can no longer
        starve the sources behind it. Before this split the adapter walked
        sources in order and transcribed inline, so five undownloadable videos
        on source #3 tripped the breaker and sources #4-#8 were never reached -
        for three runs straight on 2026-09-06.
        """
        self._log_ytdlp_version()
        candidates: list[dict] = []
        for source in self._load_sources():
            log.info("YT source: %s", source["name"])
            for v in self._fetch_source_videos(source):
                vid = v.get("id")
                if not vid or vid in seen:
                    continue
                if self._is_short(v, source.get("min_duration_s", SHORTS_MAX_SECONDS)):
                    seen.add(vid)
                    continue
                duration = v.get("duration") or 0
                if MAX_VIDEO_SECONDS and duration > MAX_VIDEO_SECONDS:
                    log.info("Skipping %s (%d min, over the %d min cap): %s",
                             vid, duration // 60, MAX_VIDEO_SECONDS // 60,
                             (v.get("title") or "")[:50])
                    seen.add(vid)
                    continue
                candidates.append({"video": v, "source": source})
        return candidates

    @staticmethod
    def _interleave(candidates: list[dict]) -> list[dict]:
        """Round-robin across sources, so no single source can spend the whole
        failure budget before the others get a turn."""
        by_source: dict[str, list[dict]] = {}
        for c in candidates:
            by_source.setdefault(c["source"]["name"], []).append(c)
        ordered: list[dict] = []
        while by_source:
            for name in list(by_source):
                ordered.append(by_source[name].pop(0))
                if not by_source[name]:
                    del by_source[name]
        return ordered

    def _fetch_metadata(self, video_id: str, private: bool = False) -> dict | None:
        """One yt-dlp metadata call - description, upload date, view count.

        Measured at about 1.6 seconds and no download. This is all discovery
        needs: the description is what Artur reads to decide whether a video is
        worth a transcript, and --flat-playlist does not return it.
        """
        cmd = [
            YTDLP_BIN, "--dump-json", "--skip-download", "--no-warnings",
            *cookies_args(private), f"https://youtu.be/{video_id}",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if r.returncode != 0 or not r.stdout.strip():
                log.warning("metadata failed for %s: %s", video_id, r.stderr[:150])
                return None
            return json.loads(r.stdout.splitlines()[0])
        except Exception as e:
            log.warning("metadata error for %s: %s", video_id, e)
            return None

    def fetch(self, state: dict) -> FetchResult:
        """Discovery only - no transcripts, no models, no spend.

        The pipeline used to transcribe everything it found and decide what was
        worth keeping afterwards, which meant paying for videos Artur never
        wanted. Now a run lists what is new and describes it; the transcript is
        fetched later, on demand, only for the videos he picks out of the
        digest. See core.transcribe.fetch_transcript.
        """
        seen = set(state.get("seen", []))
        items: list[NormalizedItem] = []
        errors: list[str] = []

        candidates = self._interleave(self._list_candidates(state, seen))
        if len(candidates) > MAX_DISCOVER_PER_RUN:
            log.info("%d candidates; describing %d this run, rest next time",
                     len(candidates), MAX_DISCOVER_PER_RUN)
            candidates = candidates[:MAX_DISCOVER_PER_RUN]

        for cand in candidates:
            v, source = cand["video"], cand["source"]
            vid = v["id"]
            private = source.get("private", False)

            meta = self._fetch_metadata(vid, private)
            if meta is None:
                # Leave it unseen so the next run retries; a metadata failure is
                # usually transient and costs nothing to repeat.
                errors.append(f"metadata-missing:{vid}")
                continue

            duration = meta.get("duration") or v.get("duration") or 0
            upload = meta.get("upload_date") or ""
            date = (f"{upload[:4]}-{upload[4:6]}-{upload[6:]}" if len(upload) == 8
                    else datetime.now(timezone.utc).date().isoformat())

            items.append(NormalizedItem(
                source_id=vid,
                url=f"https://youtu.be/{vid}",
                title=meta.get("title") or v.get("title", "Untitled"),
                author=meta.get("channel") or meta.get("uploader") or source["name"],
                date=date,
                # The description stands in for the transcript until Artur asks
                # for one. It is enough for Haiku to rank the item and enough
                # for a human to decide.
                raw_text=(meta.get("description") or "")[:10000],
                source_type="video",
                source_meta={
                    "playlist_source": source["name"],
                    "duration_s": duration,
                    "view_count": meta.get("view_count"),
                    "private_source": private,
                    "needs_transcript": True,
                },
            ))
            seen.add(vid)

        state["seen"] = sorted(seen)
        return FetchResult(new_items=items, errors=errors)
