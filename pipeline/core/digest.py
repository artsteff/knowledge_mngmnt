"""Build and send the Telegram digest via the capture bot."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

TG_MAX_LEN = 4000
BOT_TOKEN_ENV = "TELEGRAM_CAPTURE_BOT_TOKEN"
CHANNEL_ID_ENV = "TELEGRAM_CAPTURE_CHANNEL_ID"
TG_TIMEOUT = 30          # seconds per request
TG_MAX_RETRIES = 3       # attempts per message before giving up
TG_RETRY_BACKOFF = 3     # base seconds, multiplied by attempt number


@dataclass
class DigestItem:
    ref: int                    # 1..N for this digest
    score: int
    title: str
    author: str
    source_type: str
    url: str
    hook: str                   # one-line from scorer
    source_id: str              # adapter source id
    adapter: str                # adapter name
    card_slug: str | None = None  # set after summary card is written
    description: str = ""       # the author's own words, straight from the source
    duration_s: int = 0
    date: str = ""


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _icon(score: int) -> str:
    return {1: "⭐", 2: "📌"}.get(score, "·")


DESC_CHARS = 320


def _clip(text: str, limit: int = DESC_CHARS) -> str:
    """First sentences of the description, up to a limit, cut on a word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[:cut.rfind(" ")].rstrip(" ,;:—-") + "…"


def _human_duration(seconds: int) -> str:
    if not seconds:
        return ""
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


def _block(item: DigestItem) -> str:
    """One entry.

    The description is the point of this digest: it is written by whoever made
    the video, costs nothing to obtain, and is what Artur reads to decide
    whether the video is worth a transcript. The scorer's hook sits underneath
    it when there is one - and there is none when the Anthropic key is out of
    credit, which is exactly when the description has to carry the entry on its
    own.
    """
    icon = _icon(item.score)
    meta = " · ".join(x for x in (
        _html(item.author), _human_duration(item.duration_s), item.date,
    ) if x)
    lines = [
        f'\n{icon} <b>{item.ref}. <a href="{item.url}">{_html(item.title)}</a></b>',
        meta,
    ]
    if item.hook:
        lines.append(f"<i>{_html(item.hook)}</i>")
    if item.description:
        lines.append(_html(_clip(item.description)))
    return "\n".join(lines)


def build_messages(items: list[DigestItem], header_date: str) -> list[str]:
    if not items:
        return []

    header = f"📥 <b>Capture — {header_date}</b>"

    msgs, current = [], header
    for it in items:
        block = _block(it)
        candidate = current + "\n" + block
        if len(candidate) > TG_MAX_LEN and current != header:
            msgs.append(current)
            current = header + "\n" + block
        else:
            current = candidate
    msgs.append(current)
    return msgs


def post(messages: list[str]) -> tuple[bool, list[int]]:
    """Send each message to Telegram with retries.

    Never raises: a network failure or API error is logged and reflected in the
    returned ``ok`` flag so the orchestrator can degrade gracefully (commit cards,
    save digest_history, retain the backlog for a later retry) instead of crashing
    before any state is persisted.
    """
    token = os.environ[BOT_TOKEN_ENV]
    chat_id = os.environ[CHANNEL_ID_ENV]
    message_ids: list[int] = []
    ok = True
    for text in messages:
        sent = False
        for attempt in range(1, TG_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=TG_TIMEOUT,
                )
            except requests.exceptions.RequestException as e:
                log.warning("Telegram sendMessage attempt %d/%d errored: %s",
                            attempt, TG_MAX_RETRIES, e)
            else:
                if resp.ok:
                    message_ids.append(resp.json()["result"]["message_id"])
                    sent = True
                    break
                log.warning("Telegram sendMessage attempt %d/%d failed: %s %s",
                            attempt, TG_MAX_RETRIES, resp.status_code, resp.text[:200])
            if attempt < TG_MAX_RETRIES:
                time.sleep(TG_RETRY_BACKOFF * attempt)
        if not sent:
            ok = False
    return ok, message_ids
