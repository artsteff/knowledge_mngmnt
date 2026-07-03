{ARTUR_PROFILE}

---

# Task: write a summary card for Artur's second brain

Write a quick-scan index card for Obsidian. NOT a deep dive — synthesis only. The full source lives elsewhere.

## Source

- Title: {title}
- Author: {author}
- Source type: {source_type}
- URL: {url}
- Date ingested: {date}

Full content:
{content}

## Output

Output STARTS with `---` and ENDS with the `→` footer line. No preamble. No "Here is the summary". No explanation. No code fences. No markdown wrapping.

Use this EXACT structure:

---
title: "{title}"
type: summary
source_type: {source_type}
participants: [{participants_or_author}]
url: {url}
date: {date}
tags: [3 to 5 tags, lowercase-kebab-case]
source_page: ""
---

<ONE sentence: who is speaking + the context. Skip "this video covers". Lead straight to the substance.>

## Main Ideas

1. <Specific insight. Name the mechanism, the data point, or the technique.>
2. <Same.>
3. <Same.>
4. <Only if distinctly different from 1–3.>
5. <Optional. Cap at 5. Three is often right.>

## Connections

- [[concept-slug]] — one line: how this extends, contradicts, or sharpens that concept.
- [[concept-slug]] — same.

## Open question for Artur

<ONE sentence. A specific question that connects this source to something concrete in his life right now. PICK THE ANGLE THAT FITS THE SOURCE BEST — do NOT default to Manychat just because it's listed first. Possible angles, in no particular order: his current work (Manychat AI-native transformation), a 2026 foundation-year priority (work infrastructure, parenthood prep, health baseline, relationship with Sasha), a career bet (Principal PM job search at AI companies, personal brand), a decision pattern he's working on (analysis paralysis, capacity math, architect vs guardian, worth ≠ productivity), or an energy/life thread (what depletes vs energizes him, recovery formula).>

→ [[link-slug]] · [[link-slug]]

## Rules with examples

### Tags (pick 3 to 5)

- GOOD: `claude-code`, `agentic-ux`, `multi-agent-orchestration`, `plg`, `b2b-saas`, `prompt-eval`, `principal-pm`, `personal-brand-tactics`, `agent-self-healing`
- BAD: `ai`, `productivity`, `tech`, `business`, `interesting`, `general`, `video`

Pick tags Artur would actually search for next month. If a tag is so broad it would return half the wiki, drop it.

### Main Ideas — write each as Subject + Verb + concrete object/number/method

- GOOD: "Peter Yang shipped a `/slides` Claude Code skill that runs a sub-agent to screenshot each slide and self-correct layout bugs before delivery."
- GOOD: "EXO Labs hit 250 tokens/sec on a single M4 Mac by hand-tuning inference kernels — a 4× speedup vs llama.cpp on the same hardware."
- BAD: "The video discusses how AI can be used to make slides better."
- BAD: "An interesting approach to thinking about agents and their architecture."

If the source is light, three sharp ideas beats five vague ones.

### Connections

- 2 to 4 wikilinks max. Skip the section if nothing genuinely connects.
- If you don't know an existing concept slug, invent one in lowercase-kebab-case. The wiki reconciles later.
- One line per connection. Say *how* it connects, not just that it does.
- GOOD: `[[agent-self-healing-qa]] — concrete example: sub-agent screenshots slides and re-emits HTML if QA fails.`
- BAD: `[[agents]] — related to agents.`

### Open question for Artur

- Anchored in something concrete from his profile — work, 2026 priorities, decision patterns, energy, career, life.
- Format that works: "If [pattern from source], what does that mean for [Artur's specific situation]?"
- Vary the angle. Manychat is ONE option among many — see the open question section above for the full list. If you've written 3 Manychat questions in a row, force a different angle.
- GOOD (work): "If a skill is 'prompt + workflow file' (as Yang shows), should Manychat's AI-native automation builder ship workflows as user-editable skill files instead of drag-and-drop nodes?"
- GOOD (career): "If Principal PM roles at AI companies require this level of agent-architecture fluency, does Artur's current AI-native work at Manychat actually count as evidence — or is he still 'doing PM' from the outside?"
- GOOD (decision pattern): "Given Artur's tendency toward analysis paralysis under overwhelm, would adopting this evaluation framework expand his option space — or finally narrow it the way he needs?"
- GOOD (life / energy): "If 2026 is a foundation year and parenthood prep is priority #2, is this kind of high-context AI experimentation an Architect move or a Guardian move dressed up as one?"
- BAD: "What are the implications of this for product management?"

### Voice

- Direct. Lead with the conclusion. No hedging.
- No corporate filler: no "leveraging synergies", "drive alignment", "moving forward".
- No AI tone: no "Certainly!", "I hope this helps", "It is worth noting that".
- Short sentences. The occasional one-liner for emphasis.

### Language

- If the source is mostly in Russian, write the whole card in Russian. Keep the same structure.
- Otherwise English.
