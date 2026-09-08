{ARTUR_PROFILE}

---

# Task: score this source

You are scoring a candidate piece of content for Artur's capture pipeline. Artur needs **fewer, sharper inputs** — not more options. He gets 3 digests a day; aim for 2–4 items per digest most days, not 8–10.

## Source

- Title: {title}
- Author / Channel: {author}
- Source type: {source_type}     # video | podcast | article | newsletter | forward | note
- URL: {url}

Description / blurb, written by the author (first 6000 chars):
{content}

## What you are deciding

**This is the author's own description, not a transcript.** Nothing has been
transcribed yet - that happens later, on request, and only for what Artur picks
out of this digest.

So the question is not "did this text deliver an insight". A description almost
never does; it is a few lines of marketing. The question is **"is the subject
worth pulling a transcript for"**, judged from the topic, the channel and the
specificity of the claims the author makes.

Judging a description by the standard meant for a transcript is what made every
one of 31 items score 3 on 2026-09-08. Do not do that.

## Scoring rubric

- **1 = strong match.** The topic sits squarely on one of Artur's threads and
  the description promises something specific - a mechanism, a number, a named
  change, a first-hand account.
- **2 = worth offering.** On-topic for one of his threads, but the description
  is vague about what it actually contains.
- **3 = skip.** Off-domain, hype with no specifics, intro-level, or a re-run of
  something he already knows.

**Score 3 only when the subject itself does not belong**, not merely because the
description is thin. A vague description of an on-topic subject is a 2.

## Output (exactly this format, no preamble, no trailing commentary)

SCORE: <1|2|3>
HOOK: <one sentence — the most concrete thing the description promises; if it promises nothing concrete, say what the subject is>
WHY_FOR_ARTUR: <one sentence — what specifically connects to his work or career bets; for score 3, what's missing that would have made this a 1>
LANG: <en|ru>
