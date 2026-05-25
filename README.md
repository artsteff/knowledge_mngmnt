# knowledge_mngmnt

Multi-source knowledge capture and synthesis pipeline for Artur Evstefeev. Runs on GitHub Actions; writes summary cards and wiki entries to [`second-brain`](https://github.com/artsteff/second-brain) via git push; talks to the user via Telegram (`@artur_capture_bot` → "Knowledge Capture" channel).

## How it works

1. **Cron 3×/day** (`poll-sources.yml`) — pluggable source adapters (YouTube / Gmail / direct-link / …) fetch new content, Haiku scores it against `config/artur_profile.md`, Sonnet writes a summary card per score≤2 item, the run commits cards to `second-brain/summaries/` and state to this repo, then posts one numbered digest to Telegram.
2. **User replies in free-form Telegram** ("dive 3", "tell me about the Karpathy one", "skip the rest"). A Cloudflare Worker forwards the message to GH Actions (`respond.yml`). A Sonnet intent router parses the reply and dispatches `dive` / `ingest` / `skip` / `ask` per referenced item.
3. **`dive`** runs the full wiki ingest (Sonnet) — `wiki/sources/`, `wiki/entities/`, `wiki/concepts/`, Personal Brand handoff check. Commits to `second-brain`. Replies in Telegram with the file URL.

Local Obsidian on Mac auto-pulls `second-brain` via the Obsidian Git plugin.

## Repo layout

```
.github/workflows/   poll-sources.yml · respond.yml · manage-state.yml
pipeline/
  adapters/          source adapters (one file per source)
  core/              score · summarize · deep_dive · intent_router · transcribe · digest · git_io
  orchestrator.py    single entry point
state/               per-adapter JSON + digest_history/<date>.json
config/              artur_profile.md · enabled_adapters.json · per-source configs
prompts/             markdown prompt templates loaded at runtime
webhook/             Cloudflare Worker source
```

## Models

| Step | Model |
|---|---|
| Score | `claude-haiku-4-5-20251001` |
| Summary, deep dive, intent router | `claude-sonnet-4-6` |
| Transcription fallback | OpenAI `whisper-1` |

## Run locally

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
export TELEGRAM_CAPTURE_BOT_TOKEN=...
export TELEGRAM_CAPTURE_CHANNEL_ID=...
export SECOND_BRAIN_PATH=/path/to/second-brain   # local checkout
python -m pipeline.orchestrator --sources youtube --dry-run
```

`--dry-run` skips Telegram post and git push.

## External setup

See `SETUP.md` for the one-time setup of the Telegram bot, GitHub repo, secrets, and Cloudflare Worker.
