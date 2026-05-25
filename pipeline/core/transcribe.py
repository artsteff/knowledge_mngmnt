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
            f"https://youtu.be/{video_id}",
        ]
        if private:
            cmd += ["--cookies-from-browser", "chrome"]
        subprocess.run(cmd, capture_output=True, timeout=60)
        vtt_files = list(tmp.glob("*.vtt"))
        if not vtt_files:
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
        # download audio
        dl_cmd = [
            YTDLP_BIN, "-x", "--audio-format", "mp3", "--no-warnings",
            "-o", str(tmp / "%(id)s.%(ext)s"),
            f"https://youtu.be/{audio_url_or_video_id}"
            if not audio_url_or_video_id.startswith("http")
            else audio_url_or_video_id,
        ]
        if private:
            dl_cmd += ["--cookies-from-browser", "chrome"]
        r = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.warning("yt-dlp audio dl failed: %s", r.stderr[:200])
            return None
        mp3 = next(iter(tmp.glob("*.mp3")), None)
        if not mp3:
            return None

        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        with open(mp3, "rb") as f:
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
