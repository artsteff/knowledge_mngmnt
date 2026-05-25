"""Single entry point for the capture pipeline.

  python -m pipeline.orchestrator --sources youtube              # poll, score, summarize, post
  python -m pipeline.orchestrator --sources youtube --dry-run    # no Telegram, no git push
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .adapters._base import NormalizedItem, SourceAdapter
from .adapters.youtube import YouTubeAdapter
from .core import digest, git_io, score, summarize

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "state"
DIGEST_HISTORY = STATE_DIR / "digest_history"
CONFIG_DIR = REPO_ROOT / "config"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("orchestrator")


def build_adapter(name: str) -> SourceAdapter:
    if name == "youtube":
        return YouTubeAdapter(CONFIG_DIR / "youtube_sources.json")
    raise ValueError(f"unknown adapter: {name}")


def load_state(adapter_name: str) -> dict:
    p = STATE_DIR / f"{adapter_name}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"seen": []}


def save_state(adapter_name: str, state: dict) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{adapter_name}.json"
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    return p


def save_digest_history(snapshot: dict) -> Path:
    DIGEST_HISTORY.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).date().isoformat()
    p = DIGEST_HISTORY / f"{date_str}.json"
    existing = json.loads(p.read_text()) if p.exists() else {"runs": []}
    existing["runs"].append(snapshot)
    p.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    return p


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default=None, help="csv, e.g. youtube,gmail")
    parser.add_argument("--from-config", action="store_true",
                        help="use config/enabled_adapters.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="no Telegram, no git push")
    parser.add_argument("--max-items", type=int, default=10,
                        help="cap on digest size")
    args = parser.parse_args()

    if args.from_config:
        enabled = json.loads((CONFIG_DIR / "enabled_adapters.json").read_text())["enabled"]
    elif args.sources:
        enabled = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        log.error("Specify --sources or --from-config")
        sys.exit(2)

    log.info("Enabled adapters: %s", enabled)

    digest_items: list[digest.DigestItem] = []
    new_card_paths: list[Path] = []
    state_paths: list[Path] = []
    snapshot_runs: dict = {}

    for adapter_name in enabled:
        adapter = build_adapter(adapter_name)
        state = load_state(adapter_name)
        log.info("Fetching %s (state has %d seen)", adapter_name, len(state.get("seen", [])))
        result = adapter.fetch(state)
        log.info("%s: %d new, %d errors", adapter_name, len(result.new_items), len(result.errors))

        kept: list[NormalizedItem] = []
        scores: list[score.Score] = []
        for item in result.new_items:
            s = score.score_item(
                title=item.title, author=item.author,
                source_type=item.source_type, url=item.url,
                content=item.raw_text,
            )
            log.info("Scored %s = %d (%s)", item.source_id, s.score, s.hook[:60])
            if s.score <= 2:
                kept.append(item)
                scores.append(s)
            else:
                log.info("Dropping score-3: %s", item.title[:60])

        # Summary card per kept item
        for item, s in zip(kept, scores):
            try:
                card = summarize.write_summary_card(
                    title=item.title, author=item.author,
                    source_type=item.source_type, url=item.url,
                    date=item.date, content=item.raw_text,
                )
                slug = summarize.slugify(item.title)
                target = git_io.write_summary_card(slug, card)
                new_card_paths.append(target)
                item.source_meta["card_slug"] = slug
                item.source_meta["card_path"] = str(target.relative_to(git_io.second_brain_path()))
            except Exception as e:
                log.exception("Summary card failed for %s: %s", item.source_id, e)

        state_paths.append(save_state(adapter_name, state))

        # Collect digest items
        for item, s in zip(kept, scores):
            digest_items.append(digest.DigestItem(
                ref=0,    # filled below
                score=s.score,
                title=item.title,
                author=item.author,
                source_type=item.source_type,
                url=item.url,
                hook=s.hook,
                source_id=item.source_id,
                adapter=adapter_name,
                card_slug=item.source_meta.get("card_slug"),
            ))

        snapshot_runs[adapter_name] = {
            "fetched": len(result.new_items),
            "kept": len(kept),
            "errors": result.errors,
        }

    # Sort, cap, assign refs
    digest_items.sort(key=lambda x: (x.score, x.title.lower()))
    digest_items = digest_items[: args.max_items]
    for i, it in enumerate(digest_items, start=1):
        it.ref = i

    # Build messages
    header_date = datetime.now().strftime("%B %-d")
    messages = digest.build_messages(digest_items, header_date=header_date)

    sent_ok = True
    sent_message_ids: list[int] = []
    if args.dry_run:
        log.info("DRY RUN — would post %d Telegram message(s):", len(messages))
        for m in messages:
            log.info("---\n%s\n---", m)
    else:
        sent_ok, sent_message_ids = digest.post(messages)
        log.info("Telegram post: ok=%s, message_ids=%s", sent_ok, sent_message_ids)

    # Save digest_history snapshot for the intent router to look up later
    snapshot = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "tg_message_ids": sent_message_ids,
        "tg_post_ok": sent_ok,
        "adapters": snapshot_runs,
        "digest_map": {
            str(it.ref): {
                "adapter": it.adapter,
                "source_id": it.source_id,
                "url": it.url,
                "title": it.title,
                "author": it.author,
                "source_type": it.source_type,
                "score": it.score,
                "hook": it.hook,
                "card_slug": it.card_slug,
            }
            for it in digest_items
        },
    }
    history_path = save_digest_history(snapshot)
    log.info("Saved digest_history → %s", history_path.name)

    # Git operations
    if not args.dry_run:
        sb = git_io.second_brain_path()
        if new_card_paths:
            git_io.commit_and_push(
                sb, f"capture: {len(new_card_paths)} new card(s)", new_card_paths,
            )
        repo = REPO_ROOT
        git_io.commit_and_push(
            repo, f"state: {datetime.now(timezone.utc).date().isoformat()} run",
            state_paths + [history_path],
        )

    log.info("Done. Kept %d / fetched %d.",
             len(digest_items), sum(r["fetched"] for r in snapshot_runs.values()))


if __name__ == "__main__":
    main()
