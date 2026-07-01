---
title: "When a primitive gains a repetition dimension, audit every single-shot caller surface"
date: "2026-07-01"
category: "docs/solutions/design-patterns/"
module: "fleet_engine (iterative topology — capture / progress_runner / render)"
problem_type: "design_pattern"
component: "caller-layer"
severity: "high"
applies_when:
  - "Adding a repetition/multiplicity dimension to an existing primitive (N rounds where there was one pass)"
  - "Caller-layer code derives a per-item artifact/path/count/index that assumed a single fan-out"
  - "A new mode's happy path leaves the OLD path byte-identical, so the old path's tests stay green and prove nothing about the new one"
  - "Reviewing a change where a primitive now runs the same lanes/items more than once"
tags:
  - "single-shot-assumption"
  - "repetition-dimension"
  - "iterative-topology"
  - "caller-layer"
  - "cross-surface-regression"
  - "layered-review"
  - "cross-model-review"
---

# When a primitive gains a repetition dimension, audit every single-shot caller surface

## Context

Cadre's fleet engine ran lanes as a **single fan-out** — parallel (all at once) or sequential (a one-shot chain). Iterative topology (#18) added a **repetition dimension**: the same lanes run for N *rounds*. The engine change was clean and invariant-reviewed. Yet **four independent review gates found five bugs, and the first three were the same root bug in three different caller-layer surfaces** — code that materialized a per-lane value *once* and silently assumed one fan-out. None were caught by the per-unit tests or the engine review.

The trap: the single-fan-out path stays **byte-identical** (the feature is iterative-gated), so every existing test for parallel/sequential stays green — which feels like safety but says nothing about the new multi-round path.

## Guidance

When a primitive gains a repetition dimension (rounds, retries, batches, pages), **enumerate every caller-layer site that derives a per-item artifact, path, count, or index, and ask: "does this assume a single fan-out?"** Prime suspects:

- **filenames / paths** written per item (they collide or point at the wrong place across repetitions)
- **running tallies / counters** (they reset one field but accumulate another across repetitions → nonsense counts)
- **manifest / index entries** that name where an item's output lives
- **any "compute once, before the loop" value** that the new dimension now invalidates each iteration

Green tests on the *old* single-shot path are **not** evidence the *new* multi-repetition path is correct. Write tests that exercise ≥2 repetitions, including a mid-run drop.

## Why This Matters

Each of the three bugs was silent — wrong data or misleading telemetry, not a crash — and each shipped past the author, per-unit tests, and same-model review. They surfaced only because **different review lenses hit different surfaces**:

| Bug (same root: "assumed one fan-out") | Surface | Caught by |
|---|---|---|
| manifest `lanes[].file` named a flat `specialist-<role>.md` the iterative edge never writes (it writes `round-k/…`) → phantom pointers | capture | advisor |
| the `[cadre] lane … -> <file>` breadcrumb named the flat path, not `round-k/…` → "file is on disk" contract false | progress edge | caller-layer invariant review |
| the heartbeat reset `_total` per round but **accumulated** `_done/_failed/_skipped`, so `active = total − done − …` went 0 or **negative** from round 2 | render | Codex cross-model |

One blind spot, three surfaces, three gates. This is the concrete argument for **layered + cross-model review** over same-model per-unit review: the author (and a same-model advisor) shares the blind spot; independent lenses do not. See [[holistic-review-catches-cross-surface-regressions]].

## When to Apply

Any change that makes a primitive process the same items **more than once**: iterative/round-based execution, retry-with-accumulation, batching, pagination, streaming re-emits. Also when reviewing such a change — walk the caller layer surface by surface, don't trust the engine being pure.

## Examples

**Tally — reset one field, accumulate the rest → negative "active":**

```python
# BEFORE (single fan-out assumed): LaneLaunched replaces _total but the completion
# counters accumulate — fine for one fan-out, broken once LaneLaunched fires per round.
if isinstance(event, LaneLaunched):
    self._total = len(event.roles)          # round 2 sets _total=3 while _done still =3 → active=0, then negative

# AFTER: a LaneLaunched marks a FRESH fan-out → reset the per-round tally.
if isinstance(event, LaneLaunched):
    self._total = len(event.roles)
    self._done = self._failed = self._skipped = self._stage = 0   # no-op for single-shot paths (already 0)
```

**Path — computed once, wrong per repetition:**

```python
# BEFORE: flat filename, named the same for every round → the breadcrumb/manifest
# pointed at specialist-web.md, but the edge wrote round-1/specialist-web.md.
filename_for = lambda role: fmap[role]

# AFTER: round-aware, reads the current round at call time (shared closure cell).
filename_for = lambda role: f"{round_subdir(current_round)}/{fmap[role]}"   # iterative only; flat otherwise
```

The general remedy — enumerate the surfaces before trusting green tests — is the multiplicity-flavored sibling of [[enumerate-consumers-when-a-new-value-aliases-a-load-bearing-state]] (that one is about a new *value* aliasing a state; this one is about a new *repetition* breaking single-shot assumptions).

## Related

- A cheap **pre-build probe** validated the primitive's *value* before the executor was built and found debate's value is *process* (preserved cross-lane disagreement), not sharper *output* — flipping the flagship from `synthesize` to `collect` and avoiding building the wrong thing. A blind human check + cross-model reconcile caught the same-model advisor's confirmation bias. Full write-up: `docs/plans/2026-07-01-iterative-probe-verdict.md` (local).
