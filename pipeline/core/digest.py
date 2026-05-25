"""Build and send the Telegram digest via the capture bot."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

TG_MAX_LEN = 4000
BOT_TOKEN_ENV = "TELEGRAM_CAPTURE_BOT_TOKEN"
CHANNEL_ID_ENV = "TELEGRAM_CAPTURE_CHANNEL_ID"


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


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _icon(score: int) -> str:
    return {1: "⭐", 2: "📌"}.get(score, "·")


def _block(item: DigestItem) -> str:
    icon = _icon(item.score)
    return (
        f"\n{icon} <b>{item.ref}. {_html(item.title)}</b>"
        f"\n{_html(item.author)} · {_html(item.source_type)}"
        f"\n{_html(item.hook)}"
        f"\n{item.url}"
    )


def build_messages(items: list[DigestItem], header_date: str) -> list[str]:
    header = (
        f"📥 <b>Capture — {header_date}</b>\n"
        f"{len(items)} item{'s' if len(items) != 1 else ''}"
    )
    footer = '\n\n<i>reply: "dive N" · "skip N" · "ask N: …"</i>'
    if not items:
        return [f"📥 <b>Capture — {header_date}</b>\nNothing new today."]

    msgs, current = [], header
    for it in items:
        block = _block(it)
        candidate = current + "\n" + block
        if len(candidate) + len(footer) > TG_MAX_LEN and current != header:
            msgs.append(current + footer)
            current = header + "\n" + block
        else:
            current = candidate
    msgs.append(current + footer)
    return msgs


def post(messages: list[str]) -> tuple[bool, list[int]]:
    token = os.environ[BOT_TOKEN_ENV]
    chat_id = os.environ[CHANNEL_ID_ENV]
    message_ids: list[int] = []
    ok = True
    for text in messages:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if not resp.ok:
            log.warning("Telegram sendMessage failed: %s %s", resp.status_code, resp.text[:200])
            ok = False
            continue
        message_ids.append(resp.json()["result"]["message_id"])
    return ok, message_ids
