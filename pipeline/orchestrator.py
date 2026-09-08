"""Single entry point for the capture pipeline.

  python -m pipeline.orchestrator --sources youtube              # poll, score, summarize, post
  python -m pipeline.orchestrator --sources youtube --dry-run    # no Telegram, no Sonnet, no git push

Backlog model: scored items (score 1 or 2) accumulate in state["backlog"] across
fetch runs. Each run picks the top N from the backlog, summarizes ONLY those
(lazy Sonnet), posts, and removes them from the backlog on success.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")  # local launchd / manual runs read secrets from .env

from .adapters._base import NormalizedItem, SourceAdapter
from .adapters.youtube import YouTubeAdapter
from .core import digest, git_io, score, summarize

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


DEFAULT_MAX_PER_DIGEST = 3


def _source_kinds(adapter: SourceAdapter) -> dict[str, str]:
    """Return {source_name: "channel" | "playlist"}.

    Derived from the URL rather than configured, so it cannot drift out of sync
    when a source is added. An explicit "kind" in the sources file wins.
    """
    sources_file = getattr(adapter, "sources_file", None)
    if not sources_file or not sources_file.exists():
        return {}
    try:
        data = json.loads(sources_file.read_text())
    except json.JSONDecodeError:
        return {}
    kinds = {}
    for src in data.get("sources", []):
        url = src.get("url", "")
        kinds[src["name"]] = src.get(
            "kind", "playlist" if "playlist" in url or "list=" in url else "channel"
        )
    return kinds


def _pick_for_digest(
    backlog: list[dict], limit: int,
    caps: dict[str, int], kinds: dict[str, str],
) -> list[dict]:
    """Choose what goes in this digest: channels first, round-robin, capped.

    Three rules, in order.

    Channels before playlists. Playlists here are "AI news" and "Watch Later" -
    things Artur put in a queue himself, which can wait; the channels are the
    subscriptions he wants swept.

    Round-robin across sources, so every source places its first item before any
    source places its second. Straight (score, added_at) ordering produced a
    digest of eight on 2026-09-07 in which two sources took all eight slots,
    purely because they had been added to the backlog earliest - How I AI,
    Anthropic and Peter Yang could not appear at all until those two drained.

    At most `max_per_digest` from one source, default DEFAULT_MAX_PER_DIGEST.
    """
    by_source: dict[str, list[dict]] = {}
    for entry in sorted(backlog, key=lambda x: (x["score"], x["added_at"])):
        name = entry.get("source_name") or entry.get("author") or "unknown"
        by_source.setdefault(name, []).append(entry)

    # Within a kind, offer the source with the best-scoring head first.
    def order(names: list[str]) -> list[str]:
        return sorted(names, key=lambda n: (by_source[n][0]["score"],
                                            by_source[n][0]["added_at"]))

    channels = order([n for n in by_source if kinds.get(n, "channel") == "channel"])
    playlists = order([n for n in by_source if kinds.get(n, "channel") == "playlist"])

    picked: list[dict] = []
    for group in (channels, playlists):
        counts: dict[str, int] = {}
        while group and len(picked) < limit:
            progressed = False
            for name in list(group):
                if len(picked) >= limit:
                    break
                cap = caps.get(name, DEFAULT_MAX_PER_DIGEST)
                if counts.get(name, 0) >= cap or not by_source[name]:
                    group.remove(name)
                    continue
                picked.append(by_source[name].pop(0))
                counts[name] = counts.get(name, 0) + 1
                progressed = True
            if not progressed:
                break
    return picked


def _per_source_caps(adapter: SourceAdapter) -> dict[str, int]:
    """Return {source_name: max_per_digest} from the adapter's sources file.

    YouTube adapter stores sources in a JSON file with an optional `max_per_digest`
    per source. Other adapters that don't expose this concept return an empty dict.
    """
    sources_file = getattr(adapter, "sources_file", None)
    if not sources_file or not sources_file.exists():
        return {}
    try:
        data = json.loads(sources_file.read_text())
    except json.JSONDecodeError:
        return {}
    caps = {}
    for src in data.get("sources", []):
        cap = src.get("max_per_digest")
        if cap is not None:
            caps[src["name"]] = int(cap)
    return caps


def load_state(adapter_name: str) -> dict:
    p = STATE_DIR / f"{adapter_name}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"seen": [], "backlog": []}


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
                        help="no Telegram, no Sonnet card writes, no git push")
    parser.add_argument("--max-items", type=int, default=10,
                        help="cap on digest size per run")
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
        backlog = state.setdefault("backlog", [])
        log.info("Fetching %s (seen=%d, backlog=%d)",
                 adapter_name, len(state.get("seen", [])), len(backlog))

        # 1. Fetch new items
        result = adapter.fetch(state)
        log.info("%s: %d new fetched, %d errors", adapter_name, len(result.new_items), len(result.errors))

        # 2. Score every new item with Haiku; keep score 1 or 2 in backlog
        backlog_ids = {b["source_id"] for b in backlog}
        added_to_backlog = 0
        for item in result.new_items:
            if item.source_id in backlog_ids:
                continue  # defensive: already queued from a prior run
            s = score.score_item(
                title=item.title, author=item.author,
                source_type=item.source_type, url=item.url,
                content=item.raw_text,
            )
            log.info("Scored %s = %d (%s)", item.source_id, s.score, s.hook[:60])
            if s.score <= 2:
                backlog.append({
                    "source_id": item.source_id,
                    "title": item.title,
                    "author": item.author,
                    "url": item.url,
                    "source_type": item.source_type,
                    "date": item.date,
                    "raw_text": item.raw_text or "",
                    "score": s.score,
                    "hook": s.hook,
                    "adapter": adapter_name,
                    "source_name": item.source_meta.get("playlist_source") or item.author,
                    "duration_s": item.source_meta.get("duration_s") or 0,
                    "private_source": bool(item.source_meta.get("private_source")),
                    "added_at": datetime.now(timezone.utc).isoformat(),
                })
                added_to_backlog += 1
            else:
                log.info("Dropping score-3: %s", item.title[:60])

        # 3. Persist state immediately — adapter already mutated `seen`, and we've
        #    just appended to `backlog`. Save before any expensive call so a crash
        #    doesn't lose backlog items the adapter has already marked seen.
        state_paths.append(save_state(adapter_name, state))

        # 4. Pick top-N from backlog (lowest score first, then oldest),
        #    respecting per-source max_per_digest caps from config.
        to_post = _pick_for_digest(
            backlog, args.max_items,
            _per_source_caps(adapter), _source_kinds(adapter),
        )
        source_counts: dict[str, int] = {}
        for entry in to_post:
            src = entry.get("source_name") or entry.get("author") or "unknown"
            source_counts[src] = source_counts.get(src, 0) + 1
        log.info(
            "Backlog now %d; posting %d (per-source counts: %s)",
            len(backlog), len(to_post),
            {k: v for k, v in source_counts.items()},
        )

        snapshot_runs[adapter_name] = {
            "fetched": len(result.new_items),
            "scored_to_backlog": added_to_backlog,
            "backlog_size": len(backlog),
            "posting": len(to_post),
            "errors": result.errors,
        }

        # 5. No summary card here any more.
        #
        # A card used to be written for everything posted, which meant paying a
        # model to summarise videos Artur had not asked for - and doing it from
        # a transcript that had already cost time or money to obtain. The digest
        # is now a list of what is new, built from titles and descriptions
        # alone. The transcript and the card are produced on demand, when he
        # replies picking an item: see responder.handle_ingest.

        # 6. Collect digest items
        for entry in to_post:
            digest_items.append(digest.DigestItem(
                ref=0,
                score=entry["score"],
                title=entry["title"],
                author=entry["author"],
                source_type=entry["source_type"],
                url=entry["url"],
                hook=entry["hook"],
                source_id=entry["source_id"],
                adapter=adapter_name,
                card_slug=entry.get("card_slug"),
                description=entry.get("raw_text", ""),
                duration_s=entry.get("duration_s") or 0,
                date=entry.get("date", ""),
            ))

    # Sort across adapters, cap, assign refs
    digest_items.sort(key=lambda x: (x.score, x.title.lower()))
    digest_items = digest_items[: args.max_items]
    for i, it in enumerate(digest_items, start=1):
        it.ref = i

    # 7. Empty-skip: nothing in any backlog → no Telegram, no digest_history write
    if not digest_items:
        log.info("Nothing to post; skipping Telegram and digest_history.")
        log.info("Done. Fetched %d, all backlogs empty.",
                 sum(r["fetched"] for r in snapshot_runs.values()))
        return

    # 8. Build messages
    header_date = datetime.now().strftime("%B %-d · %H:%M")
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

    # 9. On successful post, drop posted items from each adapter's backlog
    if sent_ok and not args.dry_run:
        posted_by_adapter: dict[str, set[str]] = {}
        for it in digest_items:
            posted_by_adapter.setdefault(it.adapter, set()).add(it.source_id)
        for adapter_name, posted_ids in posted_by_adapter.items():
            state = load_state(adapter_name)
            state["backlog"] = [b for b in state.get("backlog", []) if b["source_id"] not in posted_ids]
            state_paths.append(save_state(adapter_name, state))
            log.info("Drained %d posted item(s) from %s backlog (now %d)",
                     len(posted_ids), adapter_name, len(state["backlog"]))

    # 10. Save digest_history snapshot for the responder's intent router
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
    if not args.dry_run:
        history_path = save_digest_history(snapshot)
        log.info("Saved digest_history → %s", history_path.name)

        # 11. Git operations (commits unconditional in git_io; push gated by KM_GIT_PUSH)
        sb = git_io.second_brain_path()
        if new_card_paths:
            git_io.commit_and_push(
                sb, f"capture: {len(new_card_paths)} new card(s)", new_card_paths,
            )
        git_io.commit_and_push(
            REPO_ROOT, f"state: {datetime.now(timezone.utc).date().isoformat()} run",
            state_paths + [history_path],
        )

    total_fetched = sum(r["fetched"] for r in snapshot_runs.values())
    total_errors = sum(len(r["errors"]) for r in snapshot_runs.values())
    log.info("Done. Fetched %d, posted %d (of %d in backlogs).",
             total_fetched, len(digest_items),
             sum(r["backlog_size"] for r in snapshot_runs.values()))

    # Errors with nothing fetched is a systemic block, not a quiet day. Exit
    # non-zero so the launchd wrapper reports it.
    #
    # This run used to exit 0 no matter what: between 2026-09-06 00:00 and 08:00
    # it logged "0 new fetched, 6 errors" three times in a row, tripped its
    # circuit breaker every time, and still reported success. Nobody was told.
    # A quiet day - no errors, nothing new - stays a clean exit.
    if total_errors and not total_fetched:
        log.error("%d error(s) and nothing fetched — treating the run as failed.",
                  total_errors)
        sys.exit(1)


if __name__ == "__main__":
    main()
