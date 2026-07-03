"""respond.yml entry point. Handles user replies dispatched from the webhook."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

import requests
from anthropic import Anthropic

from .core import deep_dive, git_io, intent_router

STATE_DIR = REPO_ROOT / "state"
DIGEST_HISTORY = STATE_DIR / "digest_history"
VAULT_PATH = Path(os.environ.get("VAULT_PATH", str(Path.home() / "Documents" / "vault")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("responder")

BOT_TOKEN = os.environ.get("TELEGRAM_CAPTURE_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CAPTURE_CHANNEL_ID")
SONNET = "claude-sonnet-4-6"
TG_MAX_LEN = 4000  # Telegram hard limit is 4096; leave headroom for footer


def latest_digest_map(lookback_days: int = 7) -> dict:
    """Merge digest_maps from the last N daily files, oldest-first so newer runs win on ref collisions.

    Channels reorder old digests below comments threads, so users often reply to a digest
    from a day or two ago. Looking only at the latest run misses those refs entirely.
    """
    if not DIGEST_HISTORY.exists():
        return {}
    # Files named YYYY-MM-DD.json (skip archive-*.json that manage_state.py creates).
    files = sorted(
        f for f in DIGEST_HISTORY.glob("*.json")
        if not f.stem.startswith("archive-")
    )
    if not files:
        return {}
    merged: dict = {}
    for f in files[-lookback_days:]:
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        for run in data.get("runs", []):
            merged.update(run.get("digest_map", {}))
    return merged


def tg_send(chat_id: int | str, text: str, reply_to: int | None = None) -> None:
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN missing; not sending: %s", text[:80])
        return
    payload: dict = {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=15,
        )
        if not resp.ok:
            log.error(
                "tg_send FAILED: status=%s, len=%d, preview=%r, error=%s",
                resp.status_code, len(text), text[:120], resp.text[:300],
            )
        else:
            log.info("tg_send ok: chat=%s, len=%d, msg_id=%s",
                     chat_id, len(text), resp.json().get("result", {}).get("message_id"))
    except Exception as e:
        log.error("tg_send EXCEPTION: %s, len=%d, preview=%r", e, len(text), text[:120])


def tg_react(chat_id: int | str, message_id: int, emoji: str) -> None:
    if not BOT_TOKEN:
        return
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setMessageReaction",
        json={
            "chat_id": chat_id, "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        },
        timeout=10,
    )


def handle_skip(ref: str, digest_map: dict, source_state_paths: list[Path]) -> None:
    item = digest_map.get(ref)
    if not item:
        log.info("skip: ref %s not in latest digest", ref)
        return
    adapter = item["adapter"]
    source_id = item["source_id"]
    state_path = STATE_DIR / f"{adapter}.json"
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text())
    seen = set(state.get("seen", []))
    seen.add(source_id)
    state["seen"] = sorted(seen)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    source_state_paths.append(state_path)


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def _md_to_telegram_html(body: str) -> str:
    """Convert summary card markdown body to Telegram-friendly HTML."""
    lines_out: list[str] = []
    for line in body.splitlines():
        line = line.rstrip()
        # Headers → bold
        if line.startswith("## "):
            lines_out.append(f"\n<b>{_html(line[3:])}</b>")
            continue
        if line.startswith("# "):
            lines_out.append(f"\n<b>{_html(line[2:])}</b>")
            continue
        # Escape, then re-apply **bold**
        escaped = _html(line)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        lines_out.append(escaped)
    out = "\n".join(lines_out).strip()
    # Collapse 3+ blank lines
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _format_dive_message(item: dict, card_body: str, new_wiki_pages: list[str]) -> str:
    title = _html(item.get("title", "Untitled"))
    url = item.get("url", "")
    body_html = _md_to_telegram_html(_strip_frontmatter(card_body))

    msg = f"📚 <b>#{item.get('ref', '?')}: {title}</b>\n{url}\n\n{body_html}"

    if new_wiki_pages:
        pages = "\n".join(f"• [[{p}]]" for p in new_wiki_pages)
        msg += f"\n\n<b>Wiki updated</b> ({len(new_wiki_pages)} page{'s' if len(new_wiki_pages) != 1 else ''}):\n{pages}"

    if len(msg) > TG_MAX_LEN:
        msg = msg[: TG_MAX_LEN - 20].rstrip() + "\n…<i>(truncated)</i>"
    return msg


def _already_dived(sb: Path, url: str) -> Path | None:
    """Return the wiki/sources/*.md page that already references this URL, else None.

    Used for dive idempotency: if a source page already exists for this URL,
    we skip the deep_dive Sonnet call entirely.
    """
    if not url:
        return None
    sources_dir = sb / "wiki" / "sources"
    if not sources_dir.exists():
        return None
    for p in sources_dir.glob("*.md"):
        try:
            body = p.read_text(errors="ignore")
        except OSError:
            continue
        if url in body:
            return p
    return None


def handle_dive(ref: str, digest_map: dict) -> tuple[list[Path], str | None]:
    """Run deep dive. Returns (touched_paths, optional insights message for Telegram)."""
    item = digest_map.get(ref)
    if not item:
        log.info("dive: ref %s not in latest digest", ref)
        return [], None
    sb = git_io.second_brain_path()

    # Idempotency: skip Sonnet if this source has already been dived.
    existing = _already_dived(sb, item.get("url", ""))
    if existing:
        log.info("dive: ref %s already dived → %s", ref, existing.stem)
        title = _html(item.get("title", "Untitled"))
        url = item.get("url", "")
        msg = (
            f'📚⏭ <b>#{ref}: <a href="{url}">{title}</a></b>\n'
            f"Already dived earlier — see [[{existing.stem}]]"
        )
        return [], msg

    card_slug = item.get("card_slug")
    if not card_slug:
        log.warning("dive: ref %s has no card_slug; skipping", ref)
        return [], None
    card_path = sb / "summaries" / f"{card_slug}.md"
    card_content = card_path.read_text() if card_path.exists() else ""
    # Related pages: shallow — just existing wiki sources + concepts
    related = []
    for sub in ("wiki/sources", "wiki/concepts", "wiki/entities"):
        d = sb / sub
        if d.exists():
            related.extend(p.stem for p in d.glob("*.md"))
    ops = deep_dive.run_deep_dive(card_content=card_content, raw_content="", related_pages=related[:80])
    touched = deep_dive.apply_ops(ops, second_brain=sb, vault=VAULT_PATH)

    # Build insights message: just the NEW wiki pages the deep_dive created.
    # Existing pages that were APPENDed to count as "updated", not "new" — but
    # for the Telegram message we list everything touched so Artur sees the impact.
    new_pages = sorted({
        p.stem for p in touched
        if p.suffix == ".md" and any(s in str(p) for s in ("/wiki/concepts/", "/wiki/sources/", "/wiki/entities/"))
    })
    item_with_ref = {**item, "ref": ref}
    insights = _format_dive_message(item_with_ref, card_content, new_pages)
    return touched, insights


_STOPWORDS = {
    "a", "about", "an", "and", "any", "are", "as", "at", "be", "but", "by", "can",
    "do", "does", "for", "from", "has", "have", "how", "i", "if", "in", "is", "it",
    "its", "me", "my", "of", "on", "or", "so", "than", "that", "the", "their",
    "them", "then", "there", "they", "this", "to", "was", "we", "what", "when",
    "where", "which", "who", "why", "will", "with", "would", "you", "your",
}
_BRAIN_SUBDIRS = ("wiki/concepts", "wiki/entities", "wiki/sources", "summaries")
_SEARCH_TOP_K = 5
_SEARCH_CHUNK_CHARS = 1500
_SEARCH_MIN_TERM_LEN = 4


def _extract_keywords(text: str) -> list[str]:
    """Tokenize, lowercase, drop stopwords and short tokens. Returns unique terms."""
    raw = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]+", text.lower())
    seen, out = set(), []
    for w in raw:
        if len(w) < _SEARCH_MIN_TERM_LEN or w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _search_brain(query: str, sb: Path, exclude_slug: str | None = None) -> list[tuple[Path, str, int]]:
    """Keyword-grep brain for pages relevant to query.

    Returns a list of (path, truncated_text, score) sorted by score desc. Score is
    the count of distinct query terms whose presence in the page body is non-zero.
    """
    terms = _extract_keywords(query)
    if not terms:
        return []
    hits: list[tuple[Path, str, int]] = []
    for sub in _BRAIN_SUBDIRS:
        d = sb / sub
        if not d.exists():
            continue
        for p in d.glob("*.md"):
            if exclude_slug and p.stem == exclude_slug:
                continue
            try:
                body = p.read_text(errors="ignore")
            except OSError:
                continue
            body_lower = body.lower()
            # Score = number of distinct terms that appear at least once
            score = sum(1 for t in terms if t in body_lower)
            if score > 0:
                hits.append((p, body[:_SEARCH_CHUNK_CHARS], score))
    hits.sort(key=lambda x: x[2], reverse=True)
    return hits[:_SEARCH_TOP_K]


def handle_ask(ref: str, question: str, digest_map: dict) -> str:
    """Answer Artur's question in Telegram with citations to his actual wiki content."""
    item = digest_map.get(ref) or {}
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    sb = git_io.second_brain_path()
    card_slug = item.get("card_slug", "")
    card_path = sb / "summaries" / f"{card_slug}.md"
    card_body = card_path.read_text() if card_path.exists() else ""

    # Search the rest of the brain — combine question + card title for richer keywords
    search_text = f"{question} {item.get('title', '')}"
    retrieved = _search_brain(search_text, sb, exclude_slug=card_slug)

    if retrieved:
        log.info("ask: retrieved %d related page(s) for ref=%s", len(retrieved), ref)
        context_blocks = []
        for path, body, score in retrieved:
            relpath = path.relative_to(sb)
            context_blocks.append(f"### [[{path.stem}]] (from {relpath}, score {score})\n{body}")
        context = "\n\n---\n\n".join(context_blocks)
        context_section = (
            "## Related notes from Artur's second brain "
            f"(top {len(retrieved)} by keyword match — use these for real citations):\n\n"
            f"{context}\n"
        )
    else:
        context_section = "## Related notes\n\n(No keyword matches in the brain — answer from the source card alone.)\n"

    prompt = (
        "You are answering Artur's question briefly in Telegram. Use his voice (direct, "
        "concise, no hedging, no AI tone). Max 6 sentences.\n\n"
        "Cite related wiki pages as [[slug]] inline ONLY when the slug appears in the "
        "'Related notes' section below. Do NOT invent slugs — if you want to reference "
        "a concept Artur doesn't have a page for yet, write it in plain text.\n\n"
        f"## Source item being discussed\n{json.dumps(item, ensure_ascii=False, indent=2)}\n\n"
        f"## Source summary card\n{card_body}\n\n"
        f"{context_section}\n"
        f"## Artur's question\n{question}"
    )
    msg = client.messages.create(
        model=SONNET, max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True, help="JSON string from repository_dispatch payload")
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError:
        log.error("invalid payload JSON")
        sys.exit(2)

    reply_text = payload.get("reply_text", "")
    message_id = int(payload.get("message_id", 0))
    chat_id = payload.get("chat_id")
    reply_to = payload.get("reply_to_message_id")

    digest_map = latest_digest_map()
    if not digest_map:
        log.warning("No digest_history found; cannot route")
        return

    route = intent_router.route(
        reply_text=reply_text, digest_map=digest_map,
        message_id=message_id, reply_to_message_id=reply_to,
    )
    log.info("Router parsed %d instruction(s); ack=%r", len(route.instructions), route.ack_message)

    if not route.instructions:
        return

    # --- Immediate ack so Artur knows the system received the request --------
    # Tally what's about to happen, post a "starting" message BEFORE any heavy work.
    dive_refs = [i.ref for i in route.instructions if i.action == "dive" and i.ref]
    ask_refs = [i.ref for i in route.instructions if i.action == "ask" and i.ref]
    skip_refs = [i.ref for i in route.instructions if i.action == "skip" and i.ref]

    ack_parts = []
    if dive_refs:
        eta = f"~{3 * len(dive_refs)}–{5 * len(dive_refs)} min" if len(dive_refs) > 1 else "~3–5 min"
        refs_str = " ".join(f"#{r}" for r in dive_refs)
        ack_parts.append(f"📚 diving on {refs_str} ({eta}, results stream as each finishes)")
    if ask_refs:
        refs_str = " ".join(f"#{r}" for r in ask_refs)
        ack_parts.append(f"💬 answering {refs_str}")
    if skip_refs:
        refs_str = " ".join(f"#{r}" for r in skip_refs) if len(skip_refs) <= 5 else f"{len(skip_refs)} items"
        ack_parts.append(f"⏭ skipping {refs_str}")
    if ack_parts:
        tg_send(chat_id, " · ".join(ack_parts), reply_to=message_id)

    # --- Process instructions one at a time, streaming results back ----------
    touched_paths: list[Path] = []
    state_paths: list[Path] = []

    for inst in route.instructions:
        if inst.action == "skip" and inst.ref:
            handle_skip(inst.ref, digest_map, state_paths)
            tg_react(chat_id, message_id, "⏭")
        elif inst.action == "dive" and inst.ref:
            paths, insights = handle_dive(inst.ref, digest_map)
            touched_paths.extend(paths)
            tg_react(chat_id, message_id, "📚")
            if insights:
                tg_send(chat_id, insights, reply_to=message_id)
        elif inst.action == "ask" and inst.ref:
            answer = handle_ask(inst.ref, inst.question or "", digest_map)
            tg_react(chat_id, message_id, "💬")
            tg_send(chat_id, f"<b>re: #{inst.ref}</b>\n{answer}", reply_to=message_id)
        elif inst.action == "ingest" and inst.ref:
            # Phase-1 cards are already written; nothing to do here for now
            tg_react(chat_id, message_id, "✅")

    # Commit (one combined commit at the end is fine — file ops were already applied)
    if touched_paths:
        git_io.commit_and_push(
            git_io.second_brain_path(),
            f"deep-dive: {len(touched_paths)} file op(s)",
            touched_paths,
        )
    if state_paths:
        git_io.commit_and_push(
            REPO_ROOT, "state: respond mutation", state_paths,
        )


if __name__ == "__main__":
    main()
