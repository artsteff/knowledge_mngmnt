"""Prompt loader. Materializes templates from prompts/*.md."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"
PROFILE_PATH = REPO_ROOT / "config" / "artur_profile.md"


def load_prompt(name: str, **vars) -> str:
    """Load prompts/<name>.md, substitute {ARTUR_PROFILE} and any other {placeholders}."""
    template = (PROMPTS_DIR / f"{name}.md").read_text()
    profile = PROFILE_PATH.read_text()
    text = template.replace("{ARTUR_PROFILE}", profile)
    for k, v in vars.items():
        text = text.replace("{" + k + "}", str(v))
    return text
