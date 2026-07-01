---
title: "Prove a threaded primitive actually threads: round 1 must be sibling-free, round 2+ must cite sibling-only evidence"
date: "2026-07-01"
category: "best-practices"
module: "fleet_engine (iterative topology — cross-round threading)"
problem_type: "best_practice"
component: "testing_framework"
severity: "medium"
applies_when:
  - "Validating that an iterative / multi-round fleet really threads prior-round output between lanes, not just that each lane obeys a shared 'engage the others' instruction"
  - "Dogfooding any 'the agents see each other' claim in a multi-agent system"
  - "A shared prompt could produce debate-shaped output (concessions, rebuttals) with zero actual cross-agent data flow"
  - "Deciding whether to lean on a threading / iteration feature in docs, demos, or claims"
tags:
  - "iterative-topology"
  - "debate"
  - "collect"
  - "dogfood"
  - "verification"
  - "cross-lane-threading"
  - "why-cadre"
  - "fleet-engine"
---

# Prove a threaded primitive actually threads: round 1 must be sibling-free, round 2+ must cite sibling-only evidence

## Context

Cadre's iterative topology (#18) runs the same lanes for N *rounds* and threads each lane's prior-round output to the others as data, so lanes can engage each other. The `debate` flagship's lane focuses **also** instruct each lane to "engage the other lanes' positions." That creates a validation trap: **debate-shaped output — concessions, rebuttals, "the skeptic correctly notes…" — can be pure instruction-following.** A model told to write *as if* debating will produce concessions and rebuttals whether or not it ever saw another lane's text. From the final output alone you cannot tell whether the threading pipe carries anything, or whether three lanes independently wrote debate-flavored essays. Get this wrong and you "validate" a feature that does nothing.

## Guidance

Use the one thing instruction-following cannot fake: **round 1 runs all lanes concurrently with zero prior data, so a round-1 output physically cannot reference a sibling.** Two assertions separate real threading from theater:

1. **Every round-1 opener is sibling-free** — no lane names or rebuts another (it can't; they ran at the same instant).
2. **Round 2+ outputs cite a specific particular a *sibling* introduced and the citing lane never raised itself** — a borrowed example, number, or framing. This is the smoking gun: a lane cannot rebut something it never saw unless the prior-round output was genuinely threaded in. Generic "as others might argue" does **not** count; look for a concrete borrowed particular.

If (1) and (2) both hold, the threading is mechanically real. If round-1 openers already cross-reference, the "debate" is a shared-prompt artifact and the pipe is unproven.

Two corollaries to state honestly, so a pass isn't over-claimed:

- **With `toolset: []` (no retrieval), the cross-lane effect is reasoning-level "critical engagement"** — concede / refine / rebut-on-evidence-*use* — **not fact-correction.** There are no fresh facts to check against, so don't describe it as one lane "correcting" another's wrong number (that is the sequential-chain / tool-enabled case, not this one).
- **In `collect`, stdout shows only the final-round endpoints and their cross-references; the revision *arc* (position X→Y because of Z) lives only in the `round-k/` transcript dirs.** That the endpoints reference each other on stdout is exactly what validates `collect` over `synthesize` — a synthesizer smooths those cross-references away — but stdout is not the arc; the arc is a run-folder read.

## Why This Matters

Instruction-following theater is invisible in the product: the output looks like a debate either way. Credit "the models debated" when they didn't and you have green-lit a feature built on nothing, then stacked more work on that false floor. The check is cheap (two greps over the run folder) and decisive. It is the confirmation-bias trap in a checkable form — the first-draft read of this exact dogfood was the rosier "the lanes engaged each other"; the round-1 / round-2 test is what turned a hopeful impression into evidence.

## When to Apply

- Any threaded / rounds-based primitive: debate, critique-revise, self-refine, retry-with-context — anything where round N sees round N−1.
- Any multi-agent "the agents see each other / share context" claim — the round-1-can't-reference test generalizes beyond Cadre.
- Before leaning on a threading / iteration feature in docs, demos, or a "why it's better" claim.

## Examples

From the #18 `debate` host dogfood (Grok / DeepSeek / Gemini, 3 rounds, `collect`, all lanes `toolset: []`), question = *"for a solo technical founder, is building in public a net advantage or liability?"*:

- **Round 1 — verified sibling-free (all three lanes).** Each opener argued in the abstract. The proponent (Grok) cited *its own* examples — Tailwind, tRPC, indie CLIs — and named no sibling.
- **Round 2 — the smoking gun.** The proponent opened *"The skeptic and contrarian positions correctly identify real frictions…"* and rebutted a **specific** example set — **HashiCorp / Tailscale / Linear / Laravel** — that it never raised in round 1 but the skeptic and contrarian did. A rebuttal of examples the lane never introduced is impossible unless the prior-round output was threaded in. → threading confirmed.

Cheap mechanical check over the run folder:

```bash
# (1) round-1 openers must NOT name siblings (they ran concurrently)
grep -iE "proponent|skeptic|contrarian" round-1/*.md    # expect: no cross-references

# (2) round-2+ must engage siblings — then eyeball for a SPECIFIC borrowed
#     particular, not just generic "as others say"
grep -ilE "proponent|skeptic|contrarian" round-2/*.md   # expect: all lanes
```

Contrast: had the round-1 openers already named and rebutted each other, the debate would be a shared-prompt artifact — identical-looking output, no proof the pipe carries anything.

## Related

- [When a primitive gains a repetition dimension, audit every single-shot caller surface](../design-patterns/single-fan-out-assumptions-break-when-a-primitive-gains-rounds.md) — the engineering sibling from the same #18 work (correctness of the caller layer). This doc is the verification sibling: does the behavior actually happen. That doc's Related also covers the blind-check + cross-model reconcile that caught the same-model confirmation bias.
- [Lens-decomposition drives review breadth; model-diversity adds coverage, consensus, and resilience](lens-decomposition-vs-model-diversity-in-review-fleets.md) — the other "isolate the variable in a host dogfood to validate a Cadre claim" methodology learning.
- [Specialist focus is a grounding control](../design-patterns/specialist-focus-grounding-control.md) — the focus prompt is a deliberate control knob; here it is also the confound the round-1 / round-2 test isolates away.
