"""Transcription: yt-dlp auto-subs first, OpenAI Whisper API as fallback."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")


def fetch_transcript_via_api(video_id: str) -> str | None:
    """Use youtube-transcript-api (1.x instance-based) — different endpoint than yt-dlp, friendlier to data-center IPs."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            TranscriptsDisabled, NoTranscriptFound, VideoUnavailable,
        )
    except ImportError:
        return None
    try:
        ytt = YouTubeTranscriptApi()
        try:
            fetched = ytt.fetch(video_id, languages=["en", "en-US", "en-GB"])
        except NoTranscriptFound:
            transcripts = ytt.list(video_id)
            # Last resort — first available transcript in any language.
            try:
                t = next(iter(transcripts))
            except StopIteration:
                return None
            fetched = t.fetch()
        # FetchedTranscript yields snippets with .text
        text = " ".join(getattr(s, "text", "") for s in fetched).strip()
        return text or None
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return None
    except Exception as e:
        log.warning("youtube-transcript-api failed for %s: %s", video_id, e)
        return None


def cookies_args(private_source: bool = False) -> list[str]:
    """yt-dlp cookie args.

    Priority:
      1. YT_COOKIES_FILE env var (cloud / CI) — used for ALL calls
      2. --cookies-from-browser chrome — local Mac, only for sources marked private
      3. nothing
    """
    cookies_file = os.environ.get("YT_COOKIES_FILE")
    if cookies_file and Path(cookies_file).exists():
        return ["--cookies", cookies_file]
    if private_source:
        return ["--cookies-from-browser", "chrome"]
    return []


def _clean_vtt(raw: str) -> str:
    """Dedupe VTT lines into clean prose. Ported from scripts/youtube/youtube_monitor.py."""
    lines, seen = [], set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.startswith("WEBVTT") or line.isdigit():
            continue
        if line.startswith("Kind:") or line.startswith("Language:"):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return " ".join(lines)


def fetch_youtube_autosubs(video_id: str, private: bool = False) -> str | None:
    """Try yt-dlp auto-generated subtitles. Returns clean text or None."""
    tmp = Path(tempfile.mkdtemp())
    try:
        cmd = [
            YTDLP_BIN, "--write-auto-subs", "--skip-download",
            "--sub-langs", "en", "--sub-format", "vtt",
            "--no-warnings", "-o", str(tmp / "%(id)s"),
            *cookies_args(private),
            f"https://youtu.be/{video_id}",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        vtt_files = list(tmp.glob("*.vtt"))
        if not vtt_files:
            if r.returncode != 0 and r.stderr:
                log.info("yt-dlp autosubs no VTT for %s: %s", video_id, r.stderr[:200])
            return None
        return _clean_vtt(vtt_files[0].read_text(errors="ignore")) or None
    except Exception as e:
        log.warning("yt-dlp autosubs failed for %s: %s", video_id, e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def transcribe_audio_via_openai(audio_url_or_video_id: str, private: bool = False) -> str | None:
    """Fallback: yt-dlp downloads audio, OpenAI Whisper transcribes."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY not set; cannot use Whisper fallback")
        return None

    tmp = Path(tempfile.mkdtemp())
    try:
        # Download whatever bestaudio is available. OpenAI Whisper accepts
        # mp3, mp4, mpeg, mpga, m4a, wav, webm. Forcing mp3 needs ffmpeg
        # conversion AND fails when YouTube serves a stripped manifest to
        # data-center IPs. Just take what we can get.
        url = audio_url_or_video_id if audio_url_or_video_id.startswith("http") \
            else f"https://youtu.be/{audio_url_or_video_id}"
        dl_cmd = [
            YTDLP_BIN, "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "--no-warnings", "--extractor-args", "youtube:player_client=android,web",
            "-o", str(tmp / "%(id)s.%(ext)s"),
            *cookies_args(private),
            url,
        ]
        r = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.warning("yt-dlp audio dl failed: %s", r.stderr[:200])
            return None
        audio = next(
            (p for p in tmp.iterdir()
             if p.is_file() and p.suffix.lower() in {".m4a", ".webm", ".mp3", ".mp4", ".mpga", ".wav"}),
            None,
        )
        if not audio:
            return None

        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        with open(audio, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        text = resp if isinstance(resp, str) else getattr(resp, "text", "")
        log.info("Whisper produced %d chars for %s", len(text), audio_url_or_video_id)
        return " ".join(text.split()) or None
    except Exception as e:
        log.warning("Whisper fallback error for %s: %s", audio_url_or_video_id, e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
