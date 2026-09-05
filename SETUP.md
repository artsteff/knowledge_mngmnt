# SETUP — one-time external steps

Do these in order. Each step has a verification check. After step 5 you can ship Phase 1; after step 8 you have Phase 2 (Telegram replies → wiki ingest).

---

## 1. Create the dedicated Telegram bot

Open `@BotFather` in Telegram:

1. `/newbot`
2. Name: `Artur Capture` · username: `artur_capture_bot` (or whatever's available)
3. Copy the bot token → you'll paste it as a GH Actions secret in step 5
4. `/setprivacy` → choose this bot → `Disable` (so it can read all group messages)

✅ Verify: send `/start` to the bot in a DM. Bot acknowledges.

---

## 2. Create the "Knowledge Capture" channel

1. New Channel → name "Knowledge Capture" · type **Private** · Subscribers only
2. Add `@artur_capture_bot` as an **administrator** (only "Post Messages" + "Edit/Delete Messages" needed)
3. Get the channel ID: open the channel in Telegram Web, copy the URL — it'll look like `https://web.telegram.org/k/#-1001234567890`. Channel ID is `-1001234567890` (note the `-100` prefix).

✅ Verify:
```bash
TOKEN=...      # your bot token
CHAT=-100...   # channel id
curl -s "https://api.telegram.org/bot$TOKEN/sendMessage" \
  -d chat_id="$CHAT" -d text="hello from setup"
```
Message appears in the channel.

---

## 3. Create the `knowledge_mngmnt` GitHub repo

```bash
gh repo create artsteff/knowledge_mngmnt --private --confirm
cd ~/GitHub/scripts/knowledge_mngmnt
git init -b main
git add -A
git commit -m "initial: capture & synthesis pipeline scaffolding"
git remote add origin git@github.com:artsteff/knowledge_mngmnt.git
git push -u origin main
```

✅ Verify: repo visible at https://github.com/artsteff/knowledge_mngmnt with all files.

---

## 4. Create a deploy key for `second-brain` write access

The workflow needs to push commits to `second-brain`. Generate a deploy key and add the **public** half to second-brain as a deploy key with write access; add the **private** half to `knowledge_mngmnt` as a secret.

```bash
ssh-keygen -t ed25519 -f /tmp/sb-deploy -N "" -C "knowledge-mngmnt-deploy"
cat /tmp/sb-deploy.pub        # → add to github.com/artsteff/second-brain → Settings → Deploy keys → Add (✓ Allow write)
cat /tmp/sb-deploy            # → copy this into the SECOND_BRAIN_DEPLOY_KEY secret in step 5
shred -u /tmp/sb-deploy /tmp/sb-deploy.pub
```

✅ Verify: `second-brain` repo settings shows the new deploy key with the write flag.

---

## 5. Set GH Actions secrets on `knowledge_mngmnt`

Repo → Settings → Secrets and variables → Actions → New repository secret.

Add each of:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from console.anthropic.com |
| `OPENAI_API_KEY` | from platform.openai.com |
| `TELEGRAM_CAPTURE_BOT_TOKEN` | bot token from step 1 |
| `TELEGRAM_CAPTURE_CHANNEL_ID` | channel id from step 2 (with `-100` prefix) |
| `SECOND_BRAIN_DEPLOY_KEY` | the **private** key from step 4 |
| `WEBHOOK_SHARED_SECRET` | a random string, e.g. `openssl rand -hex 24` (also used in step 8) |

✅ Verify: 6 secrets visible.

---

## 6. **First Phase-1 run** — manual

Repo → Actions → `poll-sources` → "Run workflow" → leave `dry_run` unchecked → Run.

Watch the run log. Expect:
- yt-dlp polls all 6 sources
- Haiku scoring per video (visible in logs)
- New summary cards committed to `second-brain/summaries/`
- State + digest_history committed to `knowledge_mngmnt/state/`
- Digest posts to the Capture channel

✅ Verify:
- Digest message in Telegram channel
- New files in `https://github.com/artsteff/second-brain/tree/main/summaries/`
- New `state/digest_history/<today>.json` in this repo

If it works, leave the cron alone — it'll fire at 06:00/12:00/18:00 UTC daily.

---

## 7. Vault auto-pull on the Mac

So new cards appear in Obsidian without manual `git pull`. Two options:

**Option A — Obsidian Git plugin** (recommended):
1. Open Obsidian → Settings → Community plugins → enable Restricted Mode off → Browse → install "Obsidian Git"
2. Settings → Obsidian Git → set Auto pull interval to 5 min, Commit message to "auto"

**Option B — launchd job:**
```bash
cat > ~/Library/LaunchAgents/com.artur.second-brain.pull.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.artur.second-brain.pull</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/git</string>
    <string>-C</string>
    <string>/Users/artur/GitHub/second-brain</string>
    <string>pull</string>
    <string>--ff-only</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.artur.second-brain.pull.plist
```

✅ Verify: after a poll-sources run, the new files appear in your Obsidian vault within 5 min.

---

## 8. Deploy the Cloudflare Worker (Phase 2)

1. `npm install -g wrangler` (if not already)
2. `wrangler login`
3. `cd webhook && wrangler deploy`
4. Copy the deployed URL — looks like `https://capture-webhook.<your-acct>.workers.dev`
5. Set the worker's env vars in the Cloudflare dashboard:
   - `GH_DISPATCH_TOKEN` (secret) — a new fine-grained PAT with `Contents: write` on `artsteff/knowledge_mngmnt`
   - `WEBHOOK_SHARED_SECRET` (secret) — same value as in step 5
   - `TELEGRAM_CAPTURE_CHANNEL_ID` (plaintext) — same value as in step 5
6. Set the Telegram webhook:
   ```bash
   curl -X POST "https://api.telegram.org/bot$TELEGRAM_CAPTURE_BOT_TOKEN/setWebhook" \
        -d url="https://capture-webhook.<your-acct>.workers.dev/tg-webhook" \
        -d secret_token="$WEBHOOK_SHARED_SECRET"
   ```

✅ Verify:
- `curl "https://api.telegram.org/bot$TELEGRAM_CAPTURE_BOT_TOKEN/getWebhookInfo"` shows the URL
- Reply `dive 1` in the Capture channel after a digest
- GH Actions tab shows `respond` workflow firing within seconds
- 📚 reaction appears on your reply in Telegram
- A new `wiki/sources/<slug>.md` lands in second-brain

---

## 9. Cutover from the local pipeline

Once you've seen the cloud pipeline post a digest in step 6, you can safely retire the local one:

```bash
# Archive local scripts
mkdir -p ~/GitHub/scripts/youtube/_archived/2026-05-25
mv ~/GitHub/scripts/youtube/{youtube_monitor.py,youtube_daily_summary.py,youtube_ingest.py,telegram_ingest_listener.py,telegram_offset.json} \
   ~/GitHub/scripts/youtube/_archived/2026-05-25/
cp ~/GitHub/scripts/youtube/seen_videos.json \
   ~/GitHub/scripts/youtube/_archived/2026-05-25/seen_videos.snapshot.json

# Unload launchd jobs
mkdir -p ~/Library/LaunchAgents/_disabled
for j in fetch send daily-summary ingest-listener; do
  launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.artur.youtube.$j.plist 2>/dev/null || true
  mv ~/Library/LaunchAgents/com.artur.youtube.$j.plist \
     ~/Library/LaunchAgents/_disabled/ 2>/dev/null || true
done
```

Mark the old project doc closed by adding to the top of `vault/03-projects/Project - YouTube Monitor.md`:

```markdown
> **Superseded by `github.com/artsteff/knowledge_mngmnt` on 2026-05-25.**
```

✅ Verify: `launchctl list | grep youtube` is empty.

---

## What's not in Phase 1/2 yet

- **Gmail adapter** — Phase 3. Needs Gmail API OAuth credentials added to GH secrets. Spec already in `pipeline/adapters/_base.py`.
- **Direct-link adapter** — Phase 4. Bot needs to accept `/forward <url>` or plain URL messages.
- **Telegram channel reader** — Phase 5. Requires Telethon + your user account.

Each is one `pipeline/adapters/<name>.py` + a line in `config/enabled_adapters.json`. No core changes.

---

## Daily user contract

- 3 digest messages/day in the Capture channel (08:00 / 14:00 / 20:00 Amsterdam — adjust crons in `poll-sources.yml`)
- Reply in any phrasing: `dive 3`, `tell me about the Karpathy one`, `skip the rest`, `wait, how does this connect to Manychat?`
- Bot reacts with 📚 / ✅ / ⏭ / 💬 and posts a short ack + (for `ask`) an in-channel answer
- New `wiki/sources/<slug>.md` appears in the second-brain repo and syncs to Obsidian within ~5 min
