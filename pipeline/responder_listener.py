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
# Discussion-group message id -> channel message id, for every digest the
# channel auto-forwarded into the group. Replies arrive in the group and point
# at group ids; the digest history knows only channel ids.
THREAD_MAP_FILE = STATE_DIR / "tg_thread_map.json"
THREAD_MAP_RETAIN = 2000

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


def load_thread_map() -> dict[str, int]:
    try:
        return json.loads(THREAD_MAP_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_thread_map(thread_map: dict[str, int]) -> None:
    # Keep the newest by numeric id - message ids grow, their strings do not
    # sort that way.
    keep = sorted(thread_map, key=int)[-THREAD_MAP_RETAIN:]
    THREAD_MAP_FILE.write_text(json.dumps({k: thread_map[k] for k in keep}, indent=2))


def channel_post_id(msg: dict) -> int | None:
    """The capture-channel message id a forwarded copy came from.

    Only our own channel counts: a post forwarded from elsewhere carries an id
    from another channel's sequence, which can collide with a digest's.
    """
    origin = msg.get("forward_origin") or {}
    if (origin.get("type") == "channel" and origin.get("message_id")
            and str((origin.get("chat") or {}).get("id")) == str(CHANNEL_ID)):
        return int(origin["message_id"])
    if (msg.get("forward_from_message_id")
            and str((msg.get("forward_from_chat") or {}).get("id")) == str(CHANNEL_ID)):
        return int(msg["forward_from_message_id"])
    return None


def digest_message_id(msg: dict, thread_map: dict[str, int]) -> int | None:
    """Which digest post this message answers, as a channel message id.

    Refs restart at 1 in every digest and there are several a day, so "3"
    means nothing until we know which post it was written under.
    - A comment straight under the post: reply_to_message is the forwarded
      copy, which carries the channel id itself.
    - A reply to another comment in that thread: message_thread_id is the
      group id of the forwarded copy, resolved through the thread map.
    Posts made in the channel itself are not handled here: they arrive with
    sender_chat set and main() skips them as the channel's own posts.
    """
    reply_to = msg.get("reply_to_message") or {}
    found = channel_post_id(reply_to)
    if found:
        return found
    for key in (msg.get("message_thread_id"), reply_to.get("message_id")):
        if key is not None and str(key) in thread_map:
            return int(thread_map[str(key)])
    return None


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


def dispatch_message(msg: dict, thread_map: dict[str, int]) -> None:
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return
    payload = {
        "reply_text": text,
        "chat_id": msg.get("chat", {}).get("id"),
        "message_id": msg.get("message_id"),
        "reply_to_message_id": (msg.get("reply_to_message") or {}).get("message_id"),
        "digest_message_id": digest_message_id(msg, thread_map),
        # A reply we could not bind must not fall back to the latest digest -
        # that fallback is for messages that answer nothing in particular.
        # A message in a comment thread counts even without an explicit reply.
        "is_reply": bool(msg.get("reply_to_message"))
                    or msg.get("message_thread_id") is not None,
    }
    if payload["digest_message_id"] is not None and msg.get("message_id") is not None:
        # Replies to this comment inherit its digest.
        thread_map[str(msg["message_id"])] = payload["digest_message_id"]
    log.info("Dispatching message_id=%s digest=%s text=%r",
             payload["message_id"], payload["digest_message_id"], text[:80])
    # Re-invoke responder.main() via its CLI shape so the entry point is identical
    # to what we'd send via GH repository_dispatch. argv munging keeps the surface small.
    saved_argv = sys.argv
    try:
        sys.argv = ["responder", "--payload", json.dumps(payload, ensure_ascii=False)]
        responder_main()
    except SystemExit as e:
        if e.code not in (None, 0):
            log.warning("responder exited with code %s", e.code)
            _report_failure(payload, f"responder exited with code {e.code}")
    except Exception as e:
        log.exception("responder raised for message_id=%s", payload["message_id"])
        _report_failure(payload, f"{type(e).__name__}: {e}")
    finally:
        sys.argv = saved_argv


def _report_failure(payload: dict, reason: str) -> None:
    """Tell Artur his request died, in the chat where he made it.

    The offset advances whether or not a message was handled, so a failed
    request is consumed and never retried. On 2026-09-06 two deep-dive replies
    were lost exactly this way - the Anthropic key was out of credit, the
    responder raised, and from his side nothing happened at all. Silence is the
    worst possible answer to a request that was received.
    """
    chat_id = payload.get("chat_id")
    if not (BOT_TOKEN and chat_id):
        return
    reason = reason.replace("<", "&lt;").replace(">", "&gt;")[:600]
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "reply_to_message_id": payload.get("message_id"),
                "parse_mode": "HTML",
                "text": ("⚠️ Couldn't handle that — the request is lost, "
                         "please send it again once this is fixed.\n\n"
                         f"<code>{reason}</code>"),
            },
            timeout=20,
        )
    except Exception:
        log.exception("Could not report the failure back to Telegram")


def main() -> None:
    if not BOT_TOKEN or not ALLOWED_CHAT_IDS:
        log.error("TELEGRAM_CAPTURE_BOT_TOKEN missing or no chat IDs configured")
        sys.exit(1)

    offset = load_offset()
    updates = fetch_updates(offset)
    if not updates:
        return
    log.info("Received %d update(s) from offset %d", len(updates), offset)
    thread_map = load_thread_map()
    highest = offset - 1
    for u in updates:
        highest = max(highest, u["update_id"])
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or u.get("edited_channel_post")
        if not msg or not is_capture_chat(msg):
            continue
        if is_auto_forwarded_digest(msg):
            source = channel_post_id(msg)
            if source:
                thread_map[str(msg["message_id"])] = source
            log.info("Skipping auto-forwarded digest (message_id=%s, channel post %s)",
                     msg.get("message_id"), source)
            continue
        try:
            dispatch_message(msg, thread_map)
        except Exception:
            log.exception("Error handling update %s", u.get("update_id"))
    save_thread_map(thread_map)
    save_offset(highest + 1)


if __name__ == "__main__":
    main()
