"""respond.yml entry point. Handles user replies dispatched from the webhook."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from anthropic import Anthropic

from .core import deep_dive, git_io, intent_router

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "state"
DIGEST_HISTORY = STATE_DIR / "digest_history"
VAULT_PATH = Path(os.environ.get("VAULT_PATH", str(Path.home() / "Documents" / "vault")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("responder")

BOT_TOKEN = os.environ.get("TELEGRAM_CAPTURE_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CAPTURE_CHANNEL_ID")
SONNET = "claude-sonnet-4-6"


def latest_digest_map() -> dict:
    if not DIGEST_HISTORY.exists():
        return {}
    files = sorted(DIGEST_HISTORY.glob("*.json"))
    if not files:
        return {}
    # Most recent file may contain multiple runs; pick last run's digest_map
    data = json.loads(files[-1].read_text())
    runs = data.get("runs", [])
    if not runs:
        return {}
    return runs[-1].get("digest_map", {})


def tg_send(text: str, reply_to: int | None = None) -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        log.warning("Telegram creds missing; not sending: %s", text[:80])
        return
    payload: dict = {
        "chat_id": CHANNEL_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json=payload, timeout=15,
    )


def tg_react(message_id: int, emoji: str) -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        return
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setMessageReaction",
        json={
            "chat_id": CHANNEL_ID, "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        },
        timeout=10,
    )


def handle_skip(ref: str, digest_map: dict, source_state_paths: list[Path]) -> None:
    item = digest_map.get(ref)
    if not item:
        log.info("skip: ref %s not in latest digest", ref)
        return
    adapter = item["adapter"]
    source_id = item["source_id"]
    state_path = STATE_DIR / f"{adapter}.json"
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text())
    seen = set(state.get("seen", []))
    seen.add(source_id)
    state["seen"] = sorted(seen)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    source_state_paths.append(state_path)


def handle_dive(ref: str, digest_map: dict) -> list[Path]:
    item = digest_map.get(ref)
    if not item:
        log.info("dive: ref %s not in latest digest", ref)
        return []
    sb = git_io.second_brain_path()
    card_slug = item.get("card_slug")
    if not card_slug:
        log.warning("dive: ref %s has no card_slug; skipping", ref)
        return []
    card_path = sb / "summaries" / f"{card_slug}.md"
    card_content = card_path.read_text() if card_path.exists() else ""
    # Related pages: shallow — just existing wiki sources + concepts
    related = []
    for sub in ("wiki/sources", "wiki/concepts", "wiki/entities"):
        d = sb / sub
        if d.exists():
            related.extend(p.stem for p in d.glob("*.md"))
    ops = deep_dive.run_deep_dive(card_content=card_content, raw_content="", related_pages=related[:80])
    touched = deep_dive.apply_ops(ops, second_brain=sb, vault=VAULT_PATH)
    return touched


def handle_ask(ref: str, question: str, digest_map: dict) -> str:
    """Answer Artur's question in Telegram with wiki citations."""
    item = digest_map.get(ref) or {}
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    sb = git_io.second_brain_path()
    # Cheap context: card body if it exists
    card_path = sb / "summaries" / f"{item.get('card_slug', '')}.md"
    card_body = card_path.read_text() if card_path.exists() else ""
    prompt = (
        "You are answering Artur's question briefly in Telegram. Use his voice (direct, "
        "concise, no hedging, no AI tone). Cite related wiki pages as [[slug]] inline "
        "when helpful. Max 6 sentences.\n\n"
        f"Reference item:\n{json.dumps(item, ensure_ascii=False, indent=2)}\n\n"
        f"Summary card content:\n{card_body}\n\n"
        f"Question: {question}"
    )
    msg = client.messages.create(
        model=SONNET, max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True, help="JSON string from repository_dispatch payload")
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError:
        log.error("invalid payload JSON")
        sys.exit(2)

    reply_text = payload.get("reply_text", "")
    message_id = int(payload.get("message_id", 0))
    reply_to = payload.get("reply_to_message_id")

    digest_map = latest_digest_map()
    if not digest_map:
        log.warning("No digest_history found; cannot route")
        return

    route = intent_router.route(
        reply_text=reply_text, digest_map=digest_map,
        message_id=message_id, reply_to_message_id=reply_to,
    )
    log.info("Router parsed %d instruction(s); ack=%r", len(route.instructions), route.ack_message)

    if not route.instructions:
        return

    touched_paths: list[Path] = []
    state_paths: list[Path] = []
    answers: list[str] = []

    for inst in route.instructions:
        if inst.action == "skip" and inst.ref:
            handle_skip(inst.ref, digest_map, state_paths)
            tg_react(message_id, "⏭")
        elif inst.action == "dive" and inst.ref:
            paths = handle_dive(inst.ref, digest_map)
            touched_paths.extend(paths)
            tg_react(message_id, "📚")
        elif inst.action == "ask" and inst.ref:
            answer = handle_ask(inst.ref, inst.question or "", digest_map)
            answers.append(f"<b>re: #{inst.ref}</b>\n{answer}")
            tg_react(message_id, "💬")
        elif inst.action == "ingest" and inst.ref:
            # Phase-1 cards are already written; nothing to do here for now
            tg_react(message_id, "✅")

    if route.ack_message:
        tg_send(route.ack_message, reply_to=message_id)
    for a in answers:
        tg_send(a, reply_to=message_id)

    # Commit
    if touched_paths:
        git_io.commit_and_push(
            git_io.second_brain_path(),
            f"deep-dive: {len(touched_paths)} file op(s)",
            touched_paths,
        )
    if state_paths:
        git_io.commit_and_push(
            REPO_ROOT, "state: respond mutation", state_paths,
        )


if __name__ == "__main__":
    main()
