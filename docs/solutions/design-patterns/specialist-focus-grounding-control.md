---
title: "Specialist focus is a grounding control, not a topical hint"
date: "2026-06-22"
category: "docs/solutions/design-patterns/"
module: "fleets (specialist focus field)"
problem_type: "design_pattern"
component: "tooling"
severity: "high"
applies_when:
  - "A fleet specialist has web/search toolsets but returns ungrounded assertions"
  - "A lane produces zero source links despite having access to live search tools"
  - "You want to guarantee citations without switching model/provider/toolset"
  - "You need to defend against model fabrication in a research-style fleet"
  - "Writing or reviewing specialist focus fields in any fleet YAML"
tags:
  - "fleet-yaml"
  - "specialist-focus"
  - "grounding"
  - "citation"
  - "prompt-design"
  - "anti-fabrication"
  - "toolset"
  - "research-swarm"
---

# Specialist focus is a grounding control, not a topical hint

## Context

A Cadre fleet specialist carries a `focus` field alongside its `provider`, `model`, and `toolset`. The field reads like a topical description, so it is easy to treat as a hint — "tell the model what area to cover." In a live dogfood run the scan lane carried a full toolset (`web`, `x_search`, `video`, `vision`) yet returned **zero** source links. Every named framework it cited — "Adobe Mysticat," "Pathfinder," "50%+ of code is AI-generated" — was asserted as fact. The synthesizer flagged these as "unverifiable." No error was raised; the lane ran to completion and looked like research. It wasn't.

## Guidance

`focus` is a grounding control, not a topical hint. Two additions flip an ungrounded lane to a grounded one:

1. **Demand sources explicitly.** State that a real, current primary source with a link is required for every claim or item.
2. **Bless "unsourced" as an honest option.** Tell the lane to mark an item unsourced if it cannot find a link, rather than asserting it. This is the anti-fabrication hedge: a bare citation demand can push a weak model to confabulate plausible-looking URLs; the explicit fallback gives it an honest out.

**Before** (scan lane, ungrounded):

```yaml
focus: >
  Fast, broad coverage — enumerate the full landscape of options so nothing
  obvious is missed. Breadth over depth.
```

**After** (same provider / model / toolset / task — only `focus` changed):

```yaml
focus: >
  Fast, broad coverage — enumerate the full landscape of options so nothing
  obvious is missed. Breadth over depth. Cite a real, current primary source
  (with a link) for every item; if you can't find one, mark the item as
  unsourced rather than asserting it.
```

The "breadth over depth / fast" phrasing is an **anti-grounding instruction**: searching is slow and deep, so that framing suppresses it. It does not prevent grounding on its own — but combine it with no citation demand and the lane takes the path of least resistance: parametric recall.

Lanes whose focus already demanded sources (`"Primary sources, papers, docs"`, `"Real-time X / social"`) grounded correctly in the same run. **Grounding tracked the focus field, not the provider or model.**

## Why This Matters

Grounding is Cadre's core value proposition. The whole point of running a multi-model fleet with web/search toolsets is to get real, current, attributed findings that a single parametric model cannot produce. A tool-bearing lane that recites from training memory is silently worse than useless: it launders parametric claims through a pipeline that looks like research, and the synthesizer has no signal to reject them beyond "unverifiable." The user sees a formatted report. The sourcing is fiction.

The anti-fabrication hedge matters for a second reason. When the synthesizer flags a claim as "unverifiable," the instinct is to call it hallucinated. That instinct was wrong here — the Adobe Mysticat GitHub repo was real and resolved (`adobe/mysticat-ai-native-guidelines`, exact cited file path included), but the lane never linked it, so the synthesizer had no evidence to evaluate. **"Unverifiable" means uncited, not fabricated.** Explicit sourcing instructions fix both: they produce links the synthesizer can evaluate, and they give the lane permission to say "unsourced" rather than inventing a URL to satisfy an implicit expectation.

This failure mode is invisible to the unit tests, which run against fake model clients — only a live multi-model run surfaces it, and only if someone checks the lanes rather than the polished synthesis (auto memory [claude]: *dogfood before agent handoff* — fakes-pass ≠ live-works).

## When to Apply

- Any specialist whose role involves research, enumeration, or claims about the external world — `web`, `x_search`, `exa`, `firecrawl`, or similar toolsets.
- When reviewing a fleet's `focus` fields before running it: scan for absence of citation language and for anti-grounding phrasing ("fast," "broad," "quick scan," "breadth over depth" without a sourcing clause).
- When a live run returns formatted output with no links, or the synthesizer flags multiple items as "unverifiable" — treat this as a focus-field diagnosis, not a provider failure.

## Examples

Same task, same lane, only `focus` changed:

| | Before | After |
|---|---|---|
| Source links | 0 | 7 of 7 items cited |
| Named assertions | "Adobe Mysticat," "Pathfinder," "50%+" stat — all uncited | Each item has a `Primary Source:` link |
| Synthesizer verdict | "unverifiable" | Attributed |

**Verification approach:** spot-check a sample of produced URLs immediately after a run. For the after-run, 4 of 4 sampled URLs were confirmed real via fetch — the Adobe Mysticat GitHub repo (including the exact file path cited), an AWS blog post, an ICMD article, and `ai-native-transformation.com`. This is the minimum bar: if a handful of random links resolve and contain what the lane claimed, the grounding is likely real. If any confabulated URL appears in the sample, treat the entire lane's output as suspect and revise the focus.

## Related

- [[hermes-tools-are-profile-scoped]] (`docs/solutions/integration-issues/hermes-tools-are-profile-scoped.md`) — the *other* cause of a silently ungrounded lane: the tool isn't provisioned in the Hermes profile. Orthogonal to this one — here the tool is present but the focus doesn't demand its use. A lane can suffer either or both; a provisioned toolset is necessary but not sufficient for grounding.
- [[fail-closed-allowlist-for-capability-gates]] (`docs/solutions/design-patterns/fail-closed-allowlist-for-capability-gates.md`) — related framing: what a specialist is actually enabled (and here, *directed*) to do versus what the config appears to declare.
- GitHub issue #5 (deferred per-toolset verification) confirms a tool is *available* to a lane at runtime, but would still not catch an underspecified focus — runtime verification and focus design are separate guards.
