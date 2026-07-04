---
title: "Verify the consumer actually uses the property a defense provides"
date: 2026-07-04
category: best-practices
module: "fleet_engine trust-safety (text_safety.frame_body gutter defense; cadre-fleet skill's agent read-back); generalizes to any shared output/display surface with more than one consumer"
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - "Adding a defense (sanitization, framing, escaping, redaction, watermarking) to a shared output/display surface that has more than one consumer — especially when one consumer is a machine parser and another a human reader"
tags: [trust-safety, defense-in-depth, security, review-discipline, consumer-analysis, code-review, cross-cutting]
---

# Verify the consumer actually uses the property a defense provides

## Context

Cadre's #45 fast-follow (trust-safety track, PR #54) closed "report-grammar mimicry": on a combined surface — the terminal render and the collect/judge `synthesis.md` — a specialist's or synthesizer's model text sits directly beneath status rows the harness prints itself (`[ok  ] scout`, `--- role ---`). Nothing stopped a model body from printing its own flush-left `[ok  ] ghost-lane` or `--- injected role ---` line, which would render indistinguishably from a real harness row to a human reading the report.

The fix shipped as `fleet_engine.text_safety.frame_body()`: sanitize the body (the existing `sanitize()`, multiline) then prefix **every** line — including the first, which sits immediately after a `--- role ---` delimiter, and blank lines — with a non-space gutter, `BODY_GUTTER = "│ "` (U+2502). A space-indent was considered and rejected: Markdown ignores up to three leading spaces before `#` or `---`, so an indented `# Specialist:` line would still forge a heading once the body lands in a `.md` file. The gutter is routed through the two surfaces where model text interleaves with trusted harness rows — the terminal (`fleet_engine/render.py`) and the collect/judge combined capture (`fleet_engine/capture.py`) — so those surfaces now share one invariant: only a harness-printed row renders at column 0.

That invariant is real, and it holds for the consumer it was built for — a human reading the terminal or the `.md` file. It was built, reviewed (`ce-code-review`, `cadre-invariant-reviewer`), and looked, correctly, like the fix for report-grammar mimicry.

The plan review then asked a question the build review had no reason to ask: who *else* reads this surface, and how? The `cadre-fleet` skill's agent read-back is a second consumer of the same rendered report, and it doesn't read it the way a human does — its prior instructions told the agent to infer run status by pattern-matching text like `judge failed` or `[FAIL]` inside the rendered body. That's a substring/regex read, not a "what sits at column 0" read. The gutter changes what the text *looks like* on screen; it does nothing to the substrings the agent's parser keys on. Ship the framing, and the agent read-back is exactly as forgeable the day after as the day before — a defense had shipped and passed review, and the second consumer got zero benefit from it.

## Guidance

Before shipping a defense that changes a shared display or output surface, do two enumerations, not one:

1. **Enumerate every consumer of the surface**, not just the one the original report named. `enumerate-every-sink-through-one-sanitizing-chokepoint.md` already establishes "enumerate every sink" for where untrusted text *exits*; this is the mirror question for where the same surface is *read back in* — every downstream reader of the surface you're about to change.
2. **For each consumer, name the property it actually relies on** — not the property the defense adds. A human eyeballing a terminal relies on visual position (column 0 vs. indented); framing helps them. A parser doing `"judge failed" in note` or `line.startswith("[ok")` relies on a literal substring being present or absent at the byte level; framing doesn't touch that substring, so it changes nothing for that consumer. Two consumers of the same rendered text can depend on two disjoint properties — a fix aimed at one is invisible to the other unless someone checks.

When a consumer's reliance doesn't match what the defense provides, don't reapply the same defense harder — route that consumer to a channel where the guarantee is structural instead of visual. Concretely, the before → after here:

- **Before:** the `cadre-fleet` agent read-back parsed the *rendered report body* for status signals — the same text a model populates.
- **After:** `skills/cadre-fleet/SKILL.md` now instructs the agent, under "Trust the structure, not the grammar (GH #45)," to take the run's structural provenance (run outcome from top-level `status`, judge result from `grades` / `ungraded` / `parse_failed`, each lane's fate from its per-lane `ok` / `timed_out` / `skipped`) from the run folder's `manifest.json` — a typed JSON record a model can influence only through the value that lands in a field, never by injecting its own freeform grammar into it — and to treat any `[ok]` / `--- role ---` / `judge … failed`-looking line found *inside* a gutter-framed body as model text to relay, not a signal to act on. The framing defense stays exactly as shipped — it's still correct for the human reading the same report — but the machine consumer moved off the surface the defense couldn't help it with, onto one where the guarantee actually holds.

`SECURITY.md` states the resulting scope honestly instead of letting the framing fix read as closing mimicry outright: framing "defends a **column-0-anchored** consumer only — a forged token still exists *inside* the guttered body line, so an un-anchored substring grep still matches it; this is why the `cadre-fleet` agent read-back reads structural provenance (`ok`/`status`/`judge_ok`/`grades`/…) from the forgery-immune `manifest.json` rather than the report."

## Why This Matters

A control's guarantee is only as real as the number of consumers that actually rely on the property it changes. "We framed every surface where model output interleaves with trusted rows" is a true, checkable, complete-*sounding* statement — and it was still not enough, because completeness had been measured over surfaces (where does untrusted text render?), not consumers (who reads that surface, and how?). Those are different enumerations. A surface can be 100% framed and a consumer of that same surface can be 100% unprotected, simultaneously, with no contradiction — which is exactly what would have shipped had the plan review not asked the second question.

This is the dangerous failure mode specific to trust-safety work: a shipped, reviewed, tested defense creates a false sense of closure precisely because it *is* correct — for the consumer everyone was picturing. Nobody reading "model bodies are now gutter-framed on every combined surface" would guess that an agent still parses that same surface by substring match with zero added protection; the fix and the gap sit right next to each other, and only a deliberate "who else consumes this" pass surfaces it. Contrast this with an ordinary correctness bug, which usually announces itself — wrong output, a failing test. This class of gap stays silent because everything downstream keeps working exactly as it did before: a defense can ship, pass every test that checks *the defense itself*, and leave a second consumer's security posture changed by exactly zero.

## When to Apply

Any time a display or output-layer defense — sanitization, escaping, framing, redaction, watermarking, masking — changes a surface with two or more consumers. The risk is sharpest, and worth a deliberate check every time, when:

- One consumer is a human (reads visually — position, formatting, color) and another is a machine (reads mechanically — substring match, regex, a fixed-offset parse, a schema field).
- The defense changes *appearance* (what renders, and where) rather than *content* (what bytes are present or absent) — appearance-only defenses are exactly the ones a mechanical parser sees straight through.
- The surface grew organically after the defense's original design — a render path that later gained an agent-facing reader, a log that later gained a metrics scraper. The newest consumer is the one least likely to have been in mind when the defense was designed.

Skip the extra pass when the surface genuinely has one consumer, or when every consumer already reads it the same way (e.g. everyone parses the same typed field) — there's no divergence left to check for.

## Examples

**Before (#45, this repo):** `fleet_engine.text_safety.frame_body()` guttered every model-output line on the terminal render (`fleet_engine/render.py`) and the collect/judge `synthesis.md` (`fleet_engine/capture.py`), closing report-grammar mimicry for a human reader. `skills/cadre-fleet/SKILL.md` still told the agent to infer run status by scanning the *rendered report text* for phrases like `judge failed` — a substring test the gutter doesn't touch, since the forged phrase still exists inside the framed line, just no longer at column 0.

**After:** `SKILL.md`'s "Trust the structure, not the grammar" section repoints the agent's status/attribution read-back at `manifest.json`'s typed fields (top-level `status` / `grades` / `ungraded`, per-lane `ok` / `timed_out` / `skipped`) — a record a model can only ever change the *value* of, never inject freeform grammar into. `SECURITY.md` documents the resulting scope as a named, bounded limit of the framing defense (residual (a) under "Report-grammar framing limits") rather than folding it in as if framing alone had solved the whole problem.

**Generalized shape:** a web app HTML-escapes user-supplied text before rendering it in a page, closing stored-XSS for the browser. If the same escaped string later flows through a JSON API to a downstream integration, that consumer never benefits from the HTML-escaping — it doesn't render HTML, so `&lt;script&gt;` just means its data now literally contains five extra characters it didn't ask for, and if that consumer itself renders the string somewhere unescaped, the original payload is often still recoverable by un-escaping once. The fix isn't "HTML-escape harder" at the first boundary; it's the same move as above — identify what the JSON consumer actually needs (well-formed, unescaped data, with escaping applied at *its own* render boundary if and when it has one) and stop assuming one boundary's defense propagates to a sibling boundary for free.

## Related

- `enumerate-every-sink-through-one-sanitizing-chokepoint.md` — the write-side complement. That discipline funnels every place untrusted text *exits* through one chokepoint; this is the read-side mirror: enumerate every place a changed surface is *read back in*, and check what property each reader actually relies on.
- `../design-patterns/enumerate-consumers-when-a-new-value-aliases-a-load-bearing-state.md` — the same "enumerate every consumer" method for a different failure. There, a new value aliases an existing state and every consumer must be updated to interpret it; here, a defense adds a property and every consumer must be checked that it can *use* it. The fixes are near-opposite: unify consumers onto one richer channel there, split the non-benefiting consumer onto a structurally-guaranteed channel here.
- `../design-patterns/in-band-marker-nonce-defends-echo-not-injection.md` — a sibling "defends X, not Y" honest-scoping discipline. That scopes a defense by attacker sophistication (blind echo vs. semantic injection); this scopes it by consumer judgment-mechanism (visual column-0 scan vs. substring match).
- `verify-tool-use-by-effect-not-dispatch-signal.md` — the closest abstract sibling: a mechanism proven under one interaction mode silently fails to transfer to another, so verify the real effect per consumer instead of assuming the guarantee carries over.
- `../design-patterns/sanitize-trust-surface-renders-against-terminal-escapes.md` — the direct ancestor in the same `text_safety` area; #45's framing scoped down from it ("defends escape-byte spoofing, not plain-text grammar-mimicry").
