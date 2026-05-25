"""Filesystem writes to the second-brain working tree.

In GH Actions the second-brain repo is checked out alongside knowledge_mngmnt
under $GITHUB_WORKSPACE. Locally the user sets SECOND_BRAIN_PATH.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def second_brain_path() -> Path:
    p = os.environ.get("SECOND_BRAIN_PATH")
    if not p:
        raise RuntimeError("SECOND_BRAIN_PATH not set")
    path = Path(p)
    if not path.exists():
        raise RuntimeError(f"SECOND_BRAIN_PATH does not exist: {path}")
    return path


def write_summary_card(slug: str, body: str) -> Path:
    """Write to second-brain/summaries/<slug>.md, auto-suffix on collision."""
    summaries = second_brain_path() / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    target = summaries / f"{slug}.md"
    counter = 1
    while target.exists():
        target = summaries / f"{slug}-{counter}.md"
        counter += 1
    target.write_text(body, encoding="utf-8")
    log.info("Wrote summary card: %s", target)
    return target


def commit_and_push(repo: Path, message: str, paths: list[Path]) -> bool:
    """Commit specific paths and push. Returns True on success (or no-op)."""
    if not paths:
        return True
    rel = [str(p.relative_to(repo)) for p in paths]
    try:
        subprocess.run(["git", "-C", str(repo), "add", *rel], check=True)
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if diff.returncode == 0:
            log.info("No changes to commit in %s", repo)
            return True
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message,
             "--author=Knowledge Mgmt Bot <bot@users.noreply.github.com>"],
            check=True,
        )
        # Only push when in GH Actions; locally let the user push manually
        if os.environ.get("GITHUB_ACTIONS") == "true":
            subprocess.run(["git", "-C", str(repo), "push"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log.error("git op failed in %s: %s", repo, e)
        return False
