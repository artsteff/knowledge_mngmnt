{ARTUR_PROFILE}

---

# Task: parse Artur's free-form Telegram reply

Artur replied to a digest message in the "Knowledge Capture" channel. Interpret what he wants. He writes naturally — don't pattern-match, infer.

## Today's digest (mapping ref → item)

{digest_json}

## Artur's reply

`{reply_text}`

(reply_to_message_id: {reply_to_message_id}, top-level message_id: {message_id})

## Possible actions per ref

- `dive` — Artur wants the full deep ingest (wiki sources + entities + concepts).
- `ingest` — keep the summary card, no deep wiki work. (Rare; usually implied by skipping all OTHER items.)
- `skip` — Artur dismisses. State moves pending → seen.
- `ask` — Artur is asking a question about an item. The orchestrator will run another Sonnet call to answer in-channel with citations to existing wiki pages. No wiki write.

## Interpretation rules

- Match natural phrasing to actions, not literal keywords. Examples:
  - "dive into 3", "deep dive #3", "tell me more about 3", "the Karpathy one — go deep", "give me the full breakdown of 2" → `dive`
  - "skip", "skip the rest", "nothing", "boring", "не нужно", "не интересно" → `skip`
  - "what does X mean?", "wait, how does this connect to Manychat?", "is this the same as Y?", "почему он так считает?" → `ask`
- A reply can mix actions: "dive 3, skip the rest" → dive on 3 + skip on every other ref in today's digest.
- If Artur names an item by content rather than number ("the Karpathy podcast", "the agent architecture one"), match against title/author in `digest_json`.
- If the reply could be either `dive` or `ask`, prefer `ask` — cheaper and reversible.
- If the reply is unparseable, off-topic, or just chitchat — return `instructions: []` and `ack_message: ""`.
- Russian and English both fine. Mixed both fine.

## Output — JSON ONLY, no other text, no markdown fences

```
{
  "instructions": [
    {"action": "dive", "ref": "3"},
    {"action": "skip", "ref": "1"},
    {"action": "skip", "ref": "2"},
    {"action": "ask", "ref": "4", "question": "how does this relate to Manychat's AI activation work?"}
  ],
  "ack_message": "📚 diving on #3 · ⏭ skipping #1 #2 · 💬 answering #4"
}
```

The `ack_message` is what the bot will post back as confirmation. Match Artur's voice — terse, lowercase-leaning, emoji-light. Use the emoji map: 📚 dive · ✅ ingest · ⏭ skip · 💬 ask. Skip the emoji if there's only one instruction and it's obvious.
