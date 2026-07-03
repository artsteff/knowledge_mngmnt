# IDEAS — Deferred work for the capture pipeline

Living roadmap. Items here are NOT promises — they're things worth doing eventually, organized by what they'd unlock. Updated as we ship / learn.

---

## 1. Retrieval — make `ask` and `dive` brain-aware

### Option B — keyword search (shipped 2026-05-31)
✅ `responder.py:_search_brain()` greps wiki/summaries for question keywords, passes top 5 page bodies to Sonnet for `ask`. Catches exact term matches.

### Option C — RAG (semantic retrieval)
Keyword search misses paraphrased connections ("agent orchestration" ↔ "multi-agent systems", "fast inference" ↔ "tokens per second"). Build when keyword search visibly fails or brain grows >500 files.

**Implementation sketch:**
- Embed each wiki/summary file with OpenAI `text-embedding-3-small` (~$0.02 for current brain)
- Store vectors in JSON (under 500 chunks) → SQLite + sqlite-vec (under 50k) → ChromaDB/Qdrant (50k+)
- New `pipeline/core/embed.py` with `index()` and `query(text, top_k)`
- New `pipeline/reindex.py` CLI — re-embed files modified since last index
- Wire into `_search_brain` so it returns either keyword OR semantic hits
- Hook into orchestrator + responder: after writing any new card/wiki file, embed it incrementally

**Cost steady-state:** ~$0.01/day for embedding new content + ~$0.0001 per ask.

### Hybrid retrieval (later)
Combine keyword (recall on exact terms / slugs) + semantic (recall on meaning). Re-rank with reciprocal rank fusion. Worth it when both signals exist.

---

## 2. More source adapters

The adapter framework (`pipeline/adapters/_base.py`) is the whole point of the new system. Each new adapter is ~150 lines and plugs in by adding its name to `config/enabled_adapters.json`.

### Gmail adapter
Inbox/folder → digest items. Filter newsletters worth reading, drop everything else.

- Auth: Google OAuth (refresh token in `.env`)
- Triage: only specific labels (e.g. `Newsletters`, `AI`, label rules) — not the full inbox
- Normalize: HTML body → markdown via BeautifulSoup
- Skip: marketing, calendar notifications, system mail
- New file: `pipeline/adapters/gmail.py`
- State: `state/gmail.json` with `seen` list of message IDs

### Substack / RSS adapter
Generic RSS-feed reader. Substack, Stratechery, individual blogs, podcast feeds.

- Library: `feedparser`
- Config: list of feed URLs in `config/feeds.json`
- One state key per feed URL
- New file: `pipeline/adapters/rss.py`

### Telegram saved messages / channel adapter
The user already forwards interesting things to a "saved" chat. Read those.

- Requires the Capture bot to be added to a "saved" group or scrape via user token (different auth model)
- Treat each forwarded message as one item
- New file: `pipeline/adapters/telegram.py`

### Direct-link / inbox adapter
A pseudo-adapter that watches a folder or accepts `add <url>` commands in Telegram. User-controlled inbox.

- New responder action: `add <url>` → fetch metadata → score → add to backlog
- Or watch `~/Documents/vault/01-inbox/links.md` and process new lines
- New file: `pipeline/adapters/direct.py`

---

## 3. Responder UX

### Streaming ack + incremental results (shipped 2026-05-31)
✅ Acknowledge immediately on receipt, post each dive insight as it completes (not all batched at end).

### Faster polling
Currently `com.artur.km.responder.plist` runs every 5 min (`StartInterval=300`). Reducing to 60s cuts average response delay from 2.5 min → 30 sec. Negligible API cost (~2 MB/day to Telegram).

Or: switch to long-polling — `KM_TG_POLL_TIMEOUT=30` would hold the connection up to 30s waiting for new messages. Slightly more code (handle timeouts), but near-instant detection.

### Conversation threading
Right now each `ask` is independent — Sonnet has no memory of previous asks. Could keep a per-day conversation log so follow-up questions ("what about pricing for it?") work without re-stating context.

### Per-item history (`history N`)
"Show me everything I've captured about agent observability" → grep summaries + wiki for term, return list of titles + dates + URLs.

---

## 4. Daily / weekly synthesis

### Daily themes (port from old system)
The old `youtube_daily_summary.py` ran at 17:00 and produced a Sonnet-synthesized digest of all videos ingested that day, with themes + Artur's open questions. Useful when there are 5+ ingests.

- New `pipeline/synthesize.py`, scheduled in a new `com.artur.km.daily-synthesis.plist`
- Reads today's `summaries/*.md` where date matches today
- Sonnet generates "themes of the day" message
- Posts to capture channel as final message of the day

### Weekly review
Sunday morning: synthesis of the week. What got dived, what got skipped, what concepts grew. Useful as input for personal-brand content drafts.

---

## 5. Cost optimization

### Pre-filter before Haiku scoring
Right now every new item gets a Haiku scoring call. For obviously-off-domain items (a video titled "10 Best Recipes" from a cooking channel), we could skip Haiku entirely via a cheap heuristic:

- Channel allowlist/blocklist
- Title regex (e.g. titles starting with "Top 10" or "Best of" — usually entertainment)
- Duration filter (under 3 min is already filtered; under 8 min often = lower-value)

Saves a few cents/day at current volume. Real impact only if adapters multiply (Gmail, RSS = much higher volume).

### Cache summaries
If the same video appears in two sources (e.g. uploaded to a playlist AND on the channel), we'd score and summarize twice. Add a content-hash cache.

---

## 6. Operational polish

### Suppress local git commits when `KM_GIT_PUSH=0`
`git_io.commit_and_push()` always creates a local commit; only the push step is gated. With `KM_GIT_PUSH=0` we still get a commit per run, which pollutes vault history. Gate the commit step too — or use `git stash` style behavior to track changes without committing.

### Auto-retry transient failures
yt-dlp HTTP 429s, Anthropic rate limits, etc. — currently we log and move on. Light retry with exponential backoff would catch most transient issues.

### Better failure visibility
If a run errors out, nothing surfaces to Telegram. Could send a short "⚠️ poll failed at 14:00 — check logs" message so silent failures don't go unnoticed.

### Backup / restore for state
A periodic dump of `state/youtube.json` to a date-stamped backup. Easy recovery if a bad code change corrupts the backlog.

---

## 7. Quality / observability

### A/B prompt testing
Maintain two versions of `summary.haiku.md`, alternate per item, log which produced the card. Compare which Artur dove on vs skipped. Iterate prompt based on real signal.

### Eval harness
Manual: a frozen set of 20 videos with "ideal" cards Artur wrote. After any prompt change, re-summarize those 20 and diff. Catches regression.

### Cost dashboard
Log per-run token spend (Haiku scoring, Sonnet summary, Sonnet dive, Sonnet ask) to a JSON file. Weekly tally.

---

## What we explicitly do NOT plan

- **Browser extension / mobile app**: Telegram is the inbox, nothing else needed.
- **Multi-user**: this is Artur's pipeline. No tenancy.
- **Real-time push**: 2-hour polling is the cadence we want. Faster would be noise.
- **Voice replies**: cute but not the bottleneck.
- **Cloud rebuild**: the v2 cloud plan was abandoned. Mac is 24/7-available. Local-first stays.
