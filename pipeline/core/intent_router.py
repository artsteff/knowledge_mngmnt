"""Sonnet free-form reply parser. Returns structured instructions."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from .prompts import load_prompt

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"


@dataclass
class Instruction:
    action: str      # dive | ingest | skip | ask | nothing
    ref: str | None
    question: str | None = None


@dataclass
class RouterOutput:
    instructions: list[Instruction]
    ack_message: str


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def route(reply_text: str, digest_map: dict, message_id: int, reply_to_message_id: int | None) -> RouterOutput:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = load_prompt(
        "intent_router.sonnet",
        digest_json=json.dumps(digest_map, ensure_ascii=False, indent=2),
        reply_text=reply_text,
        message_id=message_id,
        reply_to_message_id=reply_to_message_id or "",
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    text = _strip_fences("".join(b.text for b in msg.content if b.type == "text"))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Router returned non-JSON: %s", text[:200])
        return RouterOutput(instructions=[], ack_message="")
    instructions = [
        Instruction(
            action=item.get("action", "nothing"),
            ref=str(item["ref"]) if item.get("ref") is not None else None,
            question=item.get("question"),
        )
        for item in data.get("instructions", [])
    ]
    return RouterOutput(
        instructions=instructions,
        ack_message=data.get("ack_message", ""),
    )
