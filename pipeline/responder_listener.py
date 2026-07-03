"""Local Telegram listener — polls @artur_capture_bot, dispatches replies.

Runs every ~5 minutes via launchd (`com.artur.km.responder.plist`). Long-polls
`getUpdates`, persists the offset to `state/telegram_offset.json`, and hands
each message to the responder pipeline. The dedicated capture bot has no
other consumer (Claude plugin uses @my_cc_ai_bot), so polling is safe.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

import requests

from .responder import main as responder_main

STATE_DIR = REPO_ROOT / "state"
OFFSET_FILE = STATE_DIR / "telegram_offset.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("responder_listener")

BOT_TOKEN = os.environ.get("TELEGRAM_CAPTURE_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CAPTURE_CHANNEL_ID")
DISCUSSION_GROUP_ID = os.environ.get("TELEGRAM_CAPTURE_DISCUSSION_GROUP_ID")
ALLOWED_CHAT_IDS = {str(x) for x in (CHANNEL_ID, DISCUSSION_GROUP_ID) if x}
LONG_POLL_TIMEOUT = int(os.environ.get("KM_TG_POLL_TIMEOUT", "0"))  # 0 = short poll


def load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(json.loads(OFFSET_FILE.read_text()).get("offset", 0))
        except (json.JSONDecodeError, ValueError):
            return 0
    return 0


def save_offset(offset: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(json.dumps({"offset": offset}, indent=2))


def fetch_updates(offset: int) -> list:
    resp = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        params={
            "offset": offset,
            "timeout": LONG_POLL_TIMEOUT,
            "allowed_updates": json.dumps(["message", "channel_post", "edited_channel_post"]),
        },
        timeout=max(15, LONG_POLL_TIMEOUT + 10),
    )
    if not resp.ok:
        log.warning("getUpdates failed: %s %s", resp.status_code, resp.text[:200])
        return []
    return resp.json().get("result", [])


def is_capture_chat(msg: dict) -> bool:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    return chat_id in ALLOWED_CHAT_IDS


def is_auto_forwarded_digest(msg: dict) -> bool:
    """True if this message is the bot's own channel post auto-forwarded to the
    linked discussion group. These look like user replies but they're our own
    digest — dispatching them makes the intent router hallucinate dive commands.
    """
    if msg.get("is_automatic_forward"):
        return True
    sender_chat = msg.get("sender_chat") or {}
    if sender_chat.get("type") == "channel":
        return True
    forward_origin = msg.get("forward_origin") or {}
    if forward_origin.get("type") == "channel":
        return True
    return False


def dispatch_message(msg: dict) -> None:
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return
    payload = {
        "reply_text": text,
        "chat_id": msg.get("chat", {}).get("id"),
        "message_id": msg.get("message_id"),
        "reply_to_message_id": (msg.get("reply_to_message") or {}).get("message_id"),
    }
    log.info("Dispatching message_id=%s text=%r", payload["message_id"], text[:80])
    # Re-invoke responder.main() via its CLI shape so the entry point is identical
    # to what we'd send via GH repository_dispatch. argv munging keeps the surface small.
    saved_argv = sys.argv
    try:
        sys.argv = ["responder", "--payload", json.dumps(payload, ensure_ascii=False)]
        responder_main()
    except SystemExit as e:
        if e.code not in (None, 0):
            log.warning("responder exited with code %s", e.code)
    except Exception:
        log.exception("responder raised for message_id=%s", payload["message_id"])
    finally:
        sys.argv = saved_argv


def main() -> None:
    if not BOT_TOKEN or not ALLOWED_CHAT_IDS:
        log.error("TELEGRAM_CAPTURE_BOT_TOKEN missing or no chat IDs configured")
        sys.exit(1)

    offset = load_offset()
    updates = fetch_updates(offset)
    if not updates:
        return
    log.info("Received %d update(s) from offset %d", len(updates), offset)
    highest = offset - 1
    for u in updates:
        highest = max(highest, u["update_id"])
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or u.get("edited_channel_post")
        if not msg or not is_capture_chat(msg):
            continue
        if is_auto_forwarded_digest(msg):
            log.info("Skipping auto-forwarded digest (message_id=%s)", msg.get("message_id"))
            continue
        try:
            dispatch_message(msg)
        except Exception:
            log.exception("Error handling update %s", u.get("update_id"))
    save_offset(highest + 1)


if __name__ == "__main__":
    main()
