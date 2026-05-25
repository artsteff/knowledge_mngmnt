"""Sonnet summary-card writer."""

from __future__ import annotations

import logging
import os
import re

from anthropic import Anthropic

from .prompts import load_prompt

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"


def slugify(text: str, max_len: int = 60) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len] or "untitled"


def write_summary_card(
    title: str, author: str, source_type: str, url: str, date: str,
    content: str,
) -> str:
    """Returns the card markdown body. Caller writes it to disk."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = load_prompt(
        "summary.sonnet",
        title=title,
        author=author,
        source_type=source_type,
        url=url,
        date=date,
        participants_or_author=author,
        content=(content or "")[:8000],
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    # Strip accidental code-fence wrapping
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text
