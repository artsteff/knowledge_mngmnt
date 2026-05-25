"""Sonnet full ingest. Reads summary card + raw content, emits file operations."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from anthropic import Anthropic

from .prompts import load_prompt

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

OP_RE = re.compile(
    r"(WRITE|UPDATE|APPEND)\s+(\S+)\s*\n<<<\s*\n(.*?)\n>>>",
    re.DOTALL,
)


@dataclass
class FileOp:
    verb: str           # WRITE | UPDATE | APPEND
    path: str           # relative path within second-brain or vault
    body: str


def run_deep_dive(card_content: str, raw_content: str, related_pages: list[str]) -> list[FileOp]:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = load_prompt(
        "deep_dive.sonnet",
        card_content=card_content,
        raw_content=(raw_content or "")[:12000],
        related_pages=", ".join(related_pages) if related_pages else "(none)",
        slug="<slug-from-card>",
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    ops: list[FileOp] = []
    for m in OP_RE.finditer(text):
        ops.append(FileOp(verb=m.group(1), path=m.group(2).strip(), body=m.group(3)))
    return ops


def apply_ops(ops: list[FileOp], second_brain: Path, vault: Path) -> list[Path]:
    """Apply file ops to disk. Returns list of touched paths."""
    touched: list[Path] = []
    for op in ops:
        # Route by path prefix
        if op.path.startswith("vault/"):
            target = vault / op.path[len("vault/"):]
        else:
            target = second_brain / op.path
        target.parent.mkdir(parents=True, exist_ok=True)

        if op.verb == "WRITE":
            target.write_text(op.body.rstrip() + "\n", encoding="utf-8")
        elif op.verb == "UPDATE":
            existing = target.read_text() if target.exists() else ""
            # append block separated by blank line
            target.write_text(existing.rstrip() + "\n\n" + op.body.rstrip() + "\n", encoding="utf-8")
        elif op.verb == "APPEND":
            existing = target.read_text() if target.exists() else ""
            target.write_text(existing.rstrip() + "\n\n" + op.body.rstrip() + "\n", encoding="utf-8")
        else:
            log.warning("Unknown op verb: %s", op.verb)
            continue
        touched.append(target)
    return touched
