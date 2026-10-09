"""State housekeeping: prune stale backlog entries, archive old digest_history files.

Runs weekly via launchd (`com.artur.km.manage-state.plist`).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "state"
DIGEST_HISTORY = STATE_DIR / "digest_history"

# There is no cap on `seen`. There used to be one (keep the last 1000), but the
# adapter stores `seen` sorted by video ID, so "the last 1000" meant the last
# 1000 alphabetically: every Sunday it dropped whichever IDs sorted first,
# regardless of age, and any of them still in a channel's listing came back as
# new. 91 of 551 posted videos were posted more than once that way, some five
# times. An ID is 11 characters; a few thousand of them cost nothing.
HISTORY_RETAIN_DAYS = 30
BACKLOG_RETAIN_DAYS = 14

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("manage_state")


def prune_backlog() -> list[Path]:
    """Drop backlog entries older than BACKLOG_RETAIN_DAYS so stuck items don't linger."""
    changed: list[Path] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKLOG_RETAIN_DAYS)
    for f in STATE_DIR.glob("*.json"):
        if f.name == "telegram_offset.json":
            continue
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("backlog"), list):
            continue
        before = len(data["backlog"])
        kept = []
        for entry in data["backlog"]:
            added_at = entry.get("added_at")
            try:
                ts = datetime.fromisoformat(added_at) if added_at else None
            except ValueError:
                ts = None
            if ts is None or ts >= cutoff:
                kept.append(entry)
        if len(kept) != before:
            data["backlog"] = kept
            f.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            changed.append(f)
            log.info("Pruned %s backlog: %d -> %d entries", f.name, before, len(kept))
    return changed


def archive_history() -> list[Path]:
    if not DIGEST_HISTORY.exists():
        return []
    cutoff = date.today() - timedelta(days=HISTORY_RETAIN_DAYS)
    changed: list[Path] = []
    for f in DIGEST_HISTORY.glob("*.json"):
        if f.stem.startswith("archive-"):
            continue
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if d >= cutoff:
            continue
        archive = DIGEST_HISTORY / f"archive-{d.strftime('%Y-%m')}.json"
        existing = json.loads(archive.read_text()) if archive.exists() else {}
        existing[f.stem] = json.loads(f.read_text())
        archive.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        f.unlink()
        changed.append(archive)
        log.info("Archived digest_history/%s.json -> %s", f.stem, archive.name)
    return changed


def main() -> None:
    backlog_pruned = prune_backlog()
    archived = archive_history()
    log.info("Done. Pruned %d backlog(s); archived %d digest file(s).",
             len(backlog_pruned), len(archived))


if __name__ == "__main__":
    main()
