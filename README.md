# knowledge_mngmnt

Multi-source knowledge capture and synthesis pipeline for Artur Evstefeev. Runs **locally on the Mac via launchd**; writes summary cards and wiki entries into the local [`second-brain`](https://github.com/artsteff/second-brain) checkout; talks to the user via Telegram (`@artur_capture_bot` → "Knowledge Capture" channel).

## How it works

1. **Poll, 6×/day** (`com.artur.km.poll` at 00:00, 04:00, 08:00, 12:00, 16:00, 20:00) runs `pipeline.orchestrator --from-config --max-items 10`. Enabled adapters (`config/enabled_adapters.json`, currently YouTube only) fetch new items. "New" means not in `seen` and uploaded within the last 30 days (`max_age_days` per source in `config/youtube_sources.json`, `null` = no limit — set for the hand-filled Watch Later and AI news playlists; default overridable via `KM_MAX_AGE_DAYS`). Haiku scores each against `config/artur_profile.md`. Items scoring 1–2 go into a backlog in `state/<adapter>.json`. Each run takes the top 10 from the backlog (by score, then age) and posts one numbered digest to Telegram — title, hook and link only; no transcript or card is made at this point. Posted items leave the backlog; the run is recorded in `state/digest_history/<date>.json`.
2. **Reply, every 5 min** (`com.artur.km.responder`) runs `pipeline.responder_listener`: polls Telegram `getUpdates` for the capture bot, keeps its offset in `state/telegram_offset.json`, and accepts messages only from the channel and its discussion group. Free-form replies ("dive 3", "tell me about the Karpathy one", "skip the rest") go to a Sonnet intent router, which dispatches `dive` / `ingest` / `skip` / `ask` per referenced item. `ingest` fetches the transcript and writes a Haiku summary card to `second-brain/summaries/`. Refs restart at 1 in every digest, so a reply is matched to the digest post it was written under (the channel post id, recovered from the discussion-group thread via `state/tg_thread_map.json`); a message that is not a reply uses the latest digest. The ack names each item's title.
3. **`dive`** fetches the transcript, runs the full wiki ingest with Sonnet — `wiki/sources/`, `wiki/entities/`, `wiki/concepts/`, Personal Brand handoff check — and replies in Telegram with the file link.
4. **Prune, Sundays 04:00** (`com.artur.km.manage-state`) runs `pipeline.manage_state`: drops backlog entries older than 14 days and archives digest history older than 30 days. `seen` is never trimmed — trimming it is what used to resurface old videos.

Every launchd job is wrapped in `~/GitHub/scripts/alerts/run-with-alerts.sh`, which alerts on failure. Logs go to `logs/launchd-<job>.{out,err}`.

### Git

The pipeline commits locally — cards to `second-brain`, state to this repo — but does not push (`KM_GIT_PUSH=0` in `.env`). Pushing is done by `com.artur.git-sync` (`~/GitHub/scripts/git-sync/`), which commits and pushes both repos every 30 minutes; those are the `sync: <timestamp>` commits. Obsidian reads `second-brain` straight from disk.

### Transcription ladder

Tried in order on `ingest` / `dive`, first one that returns text wins (`pipeline/core/transcribe.py`):

Apify captions → YouTube captions API → yt-dlp auto-subs → Groq Whisper (`whisper-large-v3-turbo`, skipped without `GROQ_API_KEY`) → OpenAI `whisper-1` → local Whisper (`base`).

## Repo layout

```
pipeline/
  adapters/              source adapters (one file per source; only youtube.py exists)
  core/                  score · summarize · deep_dive · intent_router · transcribe · digest · git_io · prompts
  orchestrator.py        poll entry point
  responder_listener.py  Telegram poller → responder.py
  responder.py           intent dispatch: dive / ingest / skip / ask
  manage_state.py        weekly state pruning
state/                   per-adapter JSON (seen + backlog) · telegram_offset.json · digest_history/<date>.json
config/                  artur_profile.md · enabled_adapters.json · youtube_sources.json
prompts/                 markdown prompt templates loaded at runtime
logs/                    launchd and manual-run logs
```

The launchd plists live in `~/GitHub/scripts/launchd/com.artur.km.*.plist` and are installed into `~/Library/LaunchAgents/`.

## Models

| Step | Model |
|---|---|
| Score, summary card | `claude-haiku-4-5-20251001` |
| Deep dive, intent router, responder | `claude-sonnet-4-6` |
| Transcription | see ladder above |

## Run locally

Secrets and paths come from `.env` (see `.env.example`), loaded via python-dotenv.

```
.venv/bin/python -m pipeline.orchestrator --sources youtube --dry-run
.venv/bin/python -m pipeline.responder_listener
```

`--dry-run` skips Telegram and git.

Reload a job after editing its plist:

```
launchctl unload ~/Library/LaunchAgents/com.artur.km.poll.plist
launchctl load   ~/Library/LaunchAgents/com.artur.km.poll.plist
```

## Not in use

- **`.github/workflows/`** (`poll-sources.yml`, `respond.yml`, `manage-state.yml`) — the original GitHub Actions setup. They target a `self-hosted, macOS` runner that is not running, so scheduled runs queue for 24 h and get cancelled. Nothing depends on them.
- **`webhook/`** — Cloudflare Worker that forwarded Telegram replies to `respond.yml`. Replaced by `responder_listener.py` polling.

`SETUP.md` still describes the GitHub Actions + Cloudflare setup.
