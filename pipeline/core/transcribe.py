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
LOCAL_WHISPER_BIN = os.environ.get(
    "LOCAL_WHISPER_BIN", "/Users/artur/GitHub/scripts/.venv/bin/whisper"
)
LOCAL_WHISPER_MODEL = os.environ.get("LOCAL_WHISPER_MODEL", "base")


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
    """yt-dlp cookie args — ONLY for sources that genuinely need a login.

    Sending cookies used to help: a logged-in view bypassed YouTube's bot
    heuristics. As of 2026-09-06 the opposite is true. Measured on the five
    videos this pipeline had been stuck on, all five behaved identically:

        no cookies                  -> format resolved, audio downloads
        --cookies-from-browser      -> "The page needs to be reloaded"

    A logged-in session is now the thing YouTube challenges, so cookies are
    reserved for private playlists (Watch Later), which cannot be read without
    them. YT_COOKIES_FILE still wins when set, for cloud/CI runs.
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


AUDIO_EXTS = {".m4a", ".webm", ".mp3", ".mp4", ".mpga", ".wav", ".opus", ".ogg"}
# Both OpenAI and Groq reject uploads over 25 MB. A 47-minute video - the
# average in this pipeline - is about 45 MB of m4a, so the upload path was
# silently unusable for most of the queue. Whisper resamples everything to
# 16 kHz mono anyway, so encoding to that before upload costs no accuracy and
# cuts the file roughly twentyfold.
API_UPLOAD_LIMIT_MB = 25


def _download_audio(video_id: str, tmp: Path, private: bool = False) -> Path | None:
    """Fetch bestaudio into `tmp`. Returns the file, or None."""
    cmd = [
        # No --extractor-args: pinning player_client=android,web is what
        # produced "Requested format is not available" on every download.
        YTDLP_BIN, "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
        "--no-warnings", "-o", str(tmp / "%(id)s.%(ext)s"),
        *cookies_args(private),
        f"https://youtu.be/{video_id}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        log.warning("yt-dlp audio dl failed for %s: %s", video_id, r.stderr[:200])
        return None
    return next((p for p in tmp.iterdir()
                 if p.is_file() and p.suffix.lower() in AUDIO_EXTS), None)


def _shrink_for_upload(audio: Path, tmp: Path) -> Path:
    """Re-encode to 16 kHz mono Opus when the file is too big to upload.

    Returns the original path if it already fits or if ffmpeg is unavailable -
    an oversized upload that gets rejected is no worse than not trying.
    """
    size_mb = audio.stat().st_size / 1_000_000
    if size_mb < API_UPLOAD_LIMIT_MB:
        return audio
    if not shutil.which("ffmpeg"):
        log.warning("%s is %.0f MB and ffmpeg is missing; uploading as-is",
                    audio.name, size_mb)
        return audio
    out = tmp / f"{audio.stem}.16k.ogg"
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio),
         "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "16k", str(out)],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0 or not out.exists():
        log.warning("ffmpeg shrink failed for %s: %s", audio.name, r.stderr[:200])
        return audio
    log.info("Shrunk %s: %.0f MB -> %.1f MB", audio.name, size_mb,
             out.stat().st_size / 1_000_000)
    return out


def _transcribe_via_openai_compatible(
    video_id: str, private: bool, *, api_key: str, base_url: str | None,
    model: str, label: str,
) -> str | None:
    """Download the audio locally, transcribe it in the cloud.

    The download has to happen here: YouTube blocks data-centre IPs, which is
    why running the whole pipeline on a VPS does not work. The transcription is
    the expensive part and that is what goes to the cloud - deliberately, so
    this laptop is not pegged for hours.
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        audio = _download_audio(video_id, tmp, private)
        if not audio:
            return None
        audio = _shrink_for_upload(audio, tmp)

        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url) if base_url \
            else OpenAI(api_key=api_key)
        with open(audio, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=model, file=f, response_format="text",
            )
        text = resp if isinstance(resp, str) else getattr(resp, "text", "")
        text = " ".join(text.split())
        if text:
            log.info("%s produced %d chars for %s", label, len(text), video_id)
        return text or None
    except Exception as e:
        log.warning("%s failed for %s: %s", label, video_id, e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def transcribe_audio_via_groq(video_id: str, private: bool = False) -> str | None:
    """Groq whisper-large-v3-turbo - the cheap, fast cloud path.

    About $0.04 per hour of audio against OpenAI's $0.36, and roughly 200x
    real-time, with large-v3 quality instead of the local `base` model. Skipped
    silently when GROQ_API_KEY is unset.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    return _transcribe_via_openai_compatible(
        video_id, private, api_key=api_key,
        base_url="https://api.groq.com/openai/v1",
        model=os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
        label="Groq whisper",
    )


def transcribe_audio_via_local_whisper(video_id: str, private: bool = False) -> str | None:
    """Local Whisper CLI. OFF by default - set KM_ALLOW_LOCAL_WHISPER=1 to enable.

    Free, but it pegs this laptop: measured at roughly 10 seconds of CPU per
    minute of audio, which is about 15 hours for the 92-hour queue that existed
    on 2026-09-06. Transcription belongs in the cloud; the audio download stays
    local because YouTube blocks data-centre IPs.

    Kept as a last resort for when there is no API key at all.
    """
    if os.environ.get("KM_ALLOW_LOCAL_WHISPER", "0") not in ("1", "true", "yes"):
        log.info("Local Whisper is disabled (KM_ALLOW_LOCAL_WHISPER unset); skipping")
        return None
    if not Path(LOCAL_WHISPER_BIN).exists():
        log.warning("Local whisper not found at %s; skipping", LOCAL_WHISPER_BIN)
        return None

    tmp = Path(tempfile.mkdtemp())
    try:
        url = f"https://youtu.be/{video_id}"
        dl_cmd = [
            # No --extractor-args: pinning player_client=android,web is what
            # produced "Requested format is not available" on every download.
            # YouTube retired the android client; letting yt-dlp pick its own
            # client order resolves formats again (verified 5/5, 2026-09-06).
            YTDLP_BIN, "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "--no-warnings",
            "-o", str(tmp / "%(id)s.%(ext)s"),
            *cookies_args(private),
            url,
        ]
        r = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.warning("yt-dlp audio dl failed for %s: %s", video_id, r.stderr[:200])
            return None
        audio = next(
            (p for p in tmp.iterdir()
             if p.is_file() and p.suffix.lower() in {".m4a", ".webm", ".mp3", ".mp4", ".wav", ".opus"}),
            None,
        )
        if not audio:
            return None

        wh_cmd = [
            LOCAL_WHISPER_BIN, str(audio),
            "--model", LOCAL_WHISPER_MODEL,
            "--language", "en",
            "--output_format", "txt",
            "--output_dir", str(tmp),
            "--fp16", "False",
        ]
        wh = subprocess.run(wh_cmd, capture_output=True, text=True, timeout=900)
        if wh.returncode != 0:
            log.warning("Local whisper failed for %s: %s", video_id, wh.stderr[:200])
            return None
        txt_files = list(tmp.glob("*.txt"))
        if not txt_files:
            return None
        text = " ".join(txt_files[0].read_text(errors="ignore").split())
        log.info("Local whisper produced %d chars for %s", len(text), video_id)
        return text or None
    except subprocess.TimeoutExpired:
        log.warning("Local whisper timed out for %s", video_id)
        return None
    except Exception as e:
        log.warning("Local whisper error for %s: %s", video_id, e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def transcribe_audio_via_openai(video_id: str, private: bool = False) -> str | None:
    """OpenAI whisper-1. Works out of the box (the key is already configured),
    but costs about nine times what Groq does for the same audio."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY not set; cannot use Whisper fallback")
        return None
    return _transcribe_via_openai_compatible(
        video_id, private, api_key=api_key, base_url=None,
        model=os.environ.get("OPENAI_WHISPER_MODEL", "whisper-1"),
        label="OpenAI whisper",
    )
