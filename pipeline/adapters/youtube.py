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

from ..core.transcribe import (
    cookies_args, fetch_transcript_via_api,
    fetch_youtube_autosubs, transcribe_audio_via_groq,
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
# A streak of failures is the signature of an IP-level rate limit, and there is
# no point hammering a blocked endpoint for the rest of the run. Whether those
# failures count against a video's retry budget is decided separately, at the
# end of fetch(), by whether anything succeeded at all.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("KM_CIRCUIT_BREAKER_THRESHOLD", "5"))
# Ceiling on videos transcribed per run. --max-items caps how many get POSTED;
# nothing capped how many were fetched, because the circuit breaker always
# aborted first. With the breaker no longer tripping on Shorts, a first run
# against a fresh backlog would attempt every candidate - 120 of them on
# 2026-09-06 - which is hours of Whisper and exactly the request volume that
# earns an IP block. Anything not reached stays unseen and is picked up by the
# next run four hours later.
MAX_TRANSCRIBE_PER_RUN = int(os.environ.get("KM_MAX_TRANSCRIBE_PER_RUN", "12"))
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
    def _is_short(v: dict) -> bool:
        """Shorts are not worth transcribing, and they cannot be detected by
        duration alone.

        A channel URL expands into several tabs, and entries from the Shorts tab
        come back from --flat-playlist with `duration: None`. The old check was
        `if duration and duration <= 180`, which `None` skips entirely - so every
        Short sailed through into transcription. Shorts rarely have captions, so
        they failed, and five of them were enough to trip the circuit breaker on
        every single run (2026-09-06). The URL is the reliable signal.
        """
        url = v.get("url") or v.get("webpage_url") or ""
        if "/shorts/" in url:
            return True
        if (v.get("playlist_title") or "").endswith("- Shorts"):
            return True
        duration = v.get("duration")
        return duration is not None and duration <= SHORTS_MAX_SECONDS

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
                if self._is_short(v):
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

    def fetch(self, state: dict) -> FetchResult:
        seen = set(state.get("seen", []))
        items: list[NormalizedItem] = []
        errors: list[str] = []
        # Pending failure increments. Committed only when the run proves the
        # network path works - see the comment at the commit step below.
        pending_failed: list[tuple[str, str]] = []  # (vid, title)
        consecutive_failures = 0
        breaker_tripped = False

        candidates = self._interleave(self._list_candidates(state, seen))
        if len(candidates) > MAX_TRANSCRIBE_PER_RUN:
            log.info("%d candidates; taking %d this run, rest next time",
                     len(candidates), MAX_TRANSCRIBE_PER_RUN)
            candidates = candidates[:MAX_TRANSCRIBE_PER_RUN]

        for cand in candidates:
            if breaker_tripped:
                break
            v, source = cand["video"], cand["source"]
            vid = v["id"]
            duration = v.get("duration") or 0

            # Rate-limit guard: space out per-video processing.
            if INTER_VIDEO_SLEEP > 0:
                time.sleep(INTER_VIDEO_SLEEP)

            title = v.get("title", "Untitled")
            channel = v.get("channel") or v.get("uploader") or source["name"]
            url = f"https://youtu.be/{vid}"
            private = source.get("private", False)

            # Captions first - free and instant when the IP is not blocked.
            # Then the cloud, cheapest first. Local Whisper is last and off by
            # default: transcription in the cloud is the whole point, this
            # laptop should not be pegged for hours.
            transcript = fetch_transcript_via_api(vid)
            if not transcript:
                log.info("youtube-transcript-api missed %s; trying yt-dlp autosubs", vid)
                transcript = fetch_youtube_autosubs(vid, private=private)
            if not transcript:
                log.info("No autosubs for %s; trying Groq", vid)
                transcript = transcribe_audio_via_groq(vid, private=private)
            if not transcript:
                log.info("Groq missed %s; trying OpenAI Whisper API", vid)
                transcript = transcribe_audio_via_openai(vid, private=private)
            if not transcript:
                log.info("OpenAI missed %s; trying local Whisper (usually disabled)", vid)
                transcript = transcribe_audio_via_local_whisper(vid, private=private)

            if not transcript:
                consecutive_failures += 1
                pending_failed.append((vid, title))
                errors.append(f"transcript-missing:{vid}")
                log.warning("No transcript for %s (consecutive=%d): %s",
                            vid, consecutive_failures, title[:60])
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "🚨 Circuit breaker tripped after %d consecutive failures — "
                        "aborting run. Likely YouTube IP rate limit.",
                        consecutive_failures,
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

        # Commit failures only when at least one video succeeded this run.
        #
        # One success proves the network path works, so the failures that
        # accompanied it are about those particular videos and deserve to count
        # against their retry budget. Zero successes means the whole run was
        # blocked, and penalising videos for that would retire good ones.
        #
        # The previous rule - never commit when the breaker trips - looked
        # safer and deadlocked instead: five permanently undownloadable videos
        # tripped the breaker every run, so their counters were never touched,
        # they were never retired, and `failed` sat empty while the same five
        # blocked every run forever.
        run_proved_working = bool(items)
        if run_proved_working:
            fails = state.setdefault("failed", {})
            for vid, title in pending_failed:
                fails[vid] = fails.get(vid, 0) + 1
                if fails[vid] >= 3:
                    log.warning("Giving up on %s after %d attempts (%s)", vid, fails[vid], title[:60])
                    seen.add(vid)
                    del fails[vid]
        elif pending_failed:
            log.warning(
                "%d failure(s) and no successes — treating as a systemic block, "
                "retry counters untouched.", len(pending_failed),
            )

        state["seen"] = sorted(seen)
        if breaker_tripped:
            errors.append("circuit-breaker-tripped")
        return FetchResult(new_items=items, errors=errors)
