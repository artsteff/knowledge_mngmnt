"""State housekeeping: prune seen lists, archive old digest_history files.

Runs weekly via launchd (`com.artur.km.manage-state.plist`).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "state"
DIGEST_HISTORY = STATE_DIR / "digest_history"

SEEN_RETAIN = 1000
HISTORY_RETAIN_DAYS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("manage_state")


def prune_seen() -> list[Path]:
    changed: list[Path] = []
    for f in STATE_DIR.glob("*.json"):
        if f.name == "telegram_offset.json":
            continue
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            log.warning("Skipping unparseable state file: %s", f.name)
            continue
        if isinstance(data, dict) and isinstance(data.get("seen"), list) and len(data["seen"]) > SEEN_RETAIN:
            before = len(data["seen"])
            data["seen"] = data["seen"][-SEEN_RETAIN:]
            f.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            changed.append(f)
            log.info("Pruned %s: %d -> %d seen IDs", f.name, before, len(data["seen"]))
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
    pruned = prune_seen()
    archived = archive_history()
    log.info("Done. Pruned %d state file(s); archived %d digest file(s).",
             len(pruned), len(archived))


if __name__ == "__main__":
    main()
