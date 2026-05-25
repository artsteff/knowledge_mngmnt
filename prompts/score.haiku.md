{ARTUR_PROFILE}

---

# Task: score this source

You are scoring a candidate piece of content for Artur's capture pipeline. Artur needs **fewer, sharper inputs** — not more options. He gets 3 digests a day; aim for 2–4 items per digest most days, not 8–10.

## Source

- Title: {title}
- Author / Channel: {author}
- Source type: {source_type}     # video | podcast | article | newsletter | forward | note
- URL: {url}

Transcript / content (first 6000 chars):
{content}

## Scoring rubric (harsh by default)

- **1 = strong match.** Specific, non-obvious, actionable for one of Artur's current bets. Likely to change how he thinks about something he's working on this week.
- **2 = adjacent.** Relevant background, decent ideas, but no new edge over what he already knows.
- **3 = skip.** Generic, off-domain, already-known, hype, intro-level, or pure entertainment.

**Default to 3** unless you can name the specific insight that earns a 1 or 2.

## Output (exactly this format, no preamble, no trailing commentary)

SCORE: <1|2|3>
HOOK: <one sentence — the most concrete idea, claim, or data point in the source>
WHY_FOR_ARTUR: <one sentence — what specifically connects to his work or career bets; for score 3, what's missing that would have made this a 1>
LANG: <en|ru>
