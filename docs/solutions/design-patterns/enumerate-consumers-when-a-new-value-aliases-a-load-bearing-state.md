---
title: "When a new value aliases a load-bearing field's existing state, enumerate every consumer"
date: "2026-06-23"
last_updated: "2026-07-01"
category: "docs/solutions/design-patterns/"
module: "fleet_engine (FleetResult.ok / convergence / status)"
problem_type: "design_pattern"
component: "engine"
severity: "high"
applies_when:
  - "Adding a new enum value / mode that makes an existing field carry a new meaning"
  - "A boolean or tri-state field (ok, synth_ok, status) is read in many places"
  - "A new success state shares a representation with an existing failure/empty state"
  - "A LATER change re-aliases a field an earlier enumeration already covered"
  - "Reviewing a change that adds a mode flag to a widely-consumed result object"
tags:
  - "aliasing"
  - "load-bearing-state"
  - "convergence"
  - "consumer-enumeration"
  - "cross-cutting-change"
  - "straggler-grep"
  - "conjunctive-status"
  - "cross-model-review"
---

# When a new value aliases a load-bearing field's existing state, enumerate every consumer

## Context

Cadre grew a second fleet shape: `collect` convergence (fan out, return the raw
attributed specialist outputs, no synthesizer). A successful collect run is
`ok=True, synthesis=None, synth_ok=None`. That state **aliases** the historical
meaning of `synthesis is None` / `synth_ok is None` — "synthesis did not happen,"
i.e. a failure. Before collect, `ok=True` *always* implied a synthesized report
existed.

The new feature's hard part was not the engine branch (a five-line early return).
It was that **`ok` / `synth_ok` / `synthesis` are read in ~7 places** — process
exit code, the render header, the render "synthesis was not attempted" preamble,
the manifest, `synthesis.md`, the `Validated` progress breadcrumb, and `cli
validate`. Every one of them, written before collect existed, would read a
*successful collect run as a failure*: wrong exit code, a "partial result" header,
a manifest a downstream agent can't distinguish from all-failed, a "synthesis was
not attempted" line on a clean run.

**It recurred, with a sharper twist (2026-07-01, sequential topology).** Adding a
`sequential` chain executor gave the *already-enumerated* `FleetStatus` field a new
meaning the original enumeration never covered: for a `sequential + synthesize/judge`
run the chain uses a **conjunctive** status, so `DEGRADED` now means either "the
convergence step failed" (its historical meaning, `synthesis`/`judge` is `None`) **or**
"the chain broke mid-run but the convergence step still succeeded over the survivors"
(new — `synthesis`/`judge` is *populated*). Three consumers still branched on the
aggregate `status` alone: two render headers claimed "no synthesis" / "judge failed"
above a rendered body, and `capture._synthesis_md` **silently discarded a successful
judge grade** from `synthesis.md`. The producer (`_run_chain`) was correct; the alias
was in the consumers — again. The failure that matters: the enumeration had been done
*at plan time for the enum*, but a **later unit re-aliased the field**, and the original
consumer list didn't cover the new meaning.

## Guidance

When a change introduces a value that makes an existing load-bearing field mean
something new, the unit of work is **the set of consumers, not the producer.**

1. **Enumerate every consumer up front, by name.** The plan listed all seven sites
   (KTD2) before any code was written. Grepping the field name (`synth_ok`,
   `\.synthesis`, `result.ok`, `synthesis is None`) across the caller layer is the
   cheap way to build that list. The producer change is trivial; the consumer list
   is the spec.
2. **Add an explicit disambiguator, don't overload the alias.** We added a
   first-class `convergence` field to the result and made every consumer read it,
   rather than trying to infer mode from the aliased fields. A successful collect
   manifest now carries `convergence: "collect"` + `synthesizer: null`; an
   all-failed synthesize manifest carries `convergence: "synthesize"` — distinct,
   not guessable.
3. **Verify with an independent straggler grep AFTER implementing — not just
   per-unit tests.** Each unit shipped with green tests, but the green suite was
   *entirely the old (synthesize) path*; it proved no regression and said nothing
   about the new state. After the consumer units landed, a single grep for every
   `ok` / `synth_ok` / `synthesis`-reading site across the caller layer, walked
   one-by-one, confirmed each reads `convergence`. That grep is the real gate.
4. **Test the absence, not just the presence.** The bug is "the old failure
   framing fires on the new success." The tests that catch it assert *absence* —
   `assertNotIn("synthesis was not attempted", rendered)` on a successful collect
   run — and *distinguishability* — the collect-success manifest is `assertNotEqual`
   to the all-failed-synthesize manifest. A test that only asserts the new header is
   present would pass while the old preamble still leaks above it.
5. **Re-run the enumeration when a *later* change adds a new meaning — and prefer a
   mechanical or cross-model walk over same-model eyes.** An enumeration done once is
   not permanent: a later unit that gives the field a new value (here, the chain's
   conjunctive `DEGRADED`) silently invalidates the earlier "every consumer reads the
   disambiguator" audit, because those consumers were only checked against the *old*
   set of meanings. This re-aliasing is exactly what same-model review misses — the
   author, the advisor, and a per-unit self-review all share one mental model of "what
   DEGRADED means," so none of them re-questions the consumers. What caught it was
   **independent, mechanical re-walking**: a purpose-built invariant reviewer that
   enumerates the consumers from scratch, and a cross-model (Codex) pass. The gate,
   restated for the recurrence case: when a change adds a meaning to an
   already-consumed field, re-list the consumers *for the new meaning* and have
   something that doesn't share the author's blind spot walk them.

## Why This Matters

A missed consumer doesn't crash — it silently mislabels. The most dangerous
miss here framed a *successful* run as a failure, which is the kind of defect
that survives a happy-path demo and surfaces only when a downstream agent parses
the exit code or the manifest. The cost asymmetry is the whole point: enumerating
seven sites up front is minutes; finding the one you missed in production is a
debugging session plus a wrong-result incident.

The plan review (cross-persona, on the plan) caught a *missing site* in the
enumeration before code existed — proof that the enumeration itself deserves
review, because "every consumer" reliably has one straggler.

## When to Apply

- Any new enum/mode value added to a type that is read in more than two places.
- Any time a new success state can be represented identically to an existing
  failure/empty state (the alias smell: "now `X is None` means two things").
- During review of such a change: ask "what reads this field?" and grep — do not
  trust that the producer diff is the whole change.

## Examples

The alias, and the disambiguator that resolves it:

| State | `ok` | `synthesis` | `synth_ok` | `convergence` |
|---|---|---|---|---|
| synthesize success | True | text | True | synthesize |
| synthesize all-failed | False | None | None | synthesize |
| **collect success** (new) | **True** | **None** | **None** | **collect** |
| collect all-failed | False | None | None | collect |

Rows 2 and 3 are indistinguishable on `ok`+`synthesis`+`synth_ok` alone; only
`convergence` separates a clean collect run from a failed synthesize run. Every
consumer reads that column.

The recurrence (conjunctive status) has the same shape — a single `status` value
carrying two meanings, resolved only by the mode-detail field:

| State (sequential + synthesize) | `status` | `synthesis` | meaning |
|---|---|---|---|
| chain completed, synth ok | SUCCESS | text | full success |
| convergence failed | DEGRADED | **None** | synthesizer ran and failed |
| **chain broke mid-run, synth over survivors** (new) | **DEGRADED** | **text** | partial result, real body |

Rows 2 and 3 share `status = DEGRADED`; only `synthesis is not None` separates
"there is a report" from "there is no report." The fix made every consumer gate on
that mode-detail (`result.synthesis is not None` / `result.judge is not None`), not
on `status` alone — the same "add/read an explicit disambiguator" move as the
`convergence` column above, one level down.

## Related

- `docs/solutions/architecture-patterns/side-effects-at-the-edge-pure-engine-core.md`
  — the engine only *branched* on convergence and returned raw data; all the
  consumer wiring (render, manifest, exit code, breadcrumb) lives at the caller
  layer, so the enumeration was a caller-layer grep, not an engine concern.
- `docs/solutions/design-patterns/normalize-str-enum-at-the-boundary.md` — the other
  `FleetStatus` footgun, also caught by the cross-model (Codex) pass that same-model
  review missed: identity (`is`) comparison on a `str`-Enum. Same field, same lesson —
  status-field changes want an independent, mechanical consumer/boundary check.
