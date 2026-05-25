"""Haiku scorer. Reads prompts/score.haiku.md, returns (score, hook, why, lang)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from anthropic import Anthropic

from .prompts import load_prompt

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"


@dataclass
class Score:
    score: int          # 1 | 2 | 3
    hook: str
    why_for_artur: str
    lang: str           # 'en' | 'ru'


def _parse(text: str) -> Score:
    fields = {"SCORE": "2", "HOOK": "", "WHY_FOR_ARTUR": "", "LANG": "en"}
    for line in text.strip().splitlines():
        for key in fields:
            prefix = key + ":"
            if line.startswith(prefix):
                fields[key] = line[len(prefix):].strip()
                break
    try:
        score_int = int(fields["SCORE"])
        if score_int not in (1, 2, 3):
            score_int = 2
    except ValueError:
        score_int = 2
    return Score(
        score=score_int,
        hook=fields["HOOK"],
        why_for_artur=fields["WHY_FOR_ARTUR"],
        lang=fields["LANG"] or "en",
    )


def score_item(
    title: str, author: str, source_type: str, url: str, content: str
) -> Score:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = load_prompt(
        "score.haiku",
        title=title,
        author=author,
        source_type=source_type,
        url=url,
        content=(content or "")[:6000],
    )
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return _parse(text)
    except Exception as e:
        log.warning("Haiku scoring failed for %s: %s", url, e)
        return Score(score=2, hook="", why_for_artur="scoring failed", lang="en")
