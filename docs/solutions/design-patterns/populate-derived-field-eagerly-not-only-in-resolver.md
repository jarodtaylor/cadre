---
title: Populate a derived field eagerly at parse, not only in a downstream resolver
date: 2026-06-24
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "You add a caller-layer resolver that populates a carrier field a pure consumer reads"
  - "The consumer reads only the carrier field, not the original input fields"
  - "Some inputs can produce the carrier value with no I/O (a cheap case alongside an expensive one)"
tags: [carrier-field, resolver, hidden-precondition, api-contract, parse-time, engine-purity]
---

# Populate a derived field eagerly at parse, not only in a downstream resolver

## Context

Personas added a caller-layer `resolve(config, pool_dir)` that populates `spec.effective_instruction` — the single field the pure engine reads — before `run_fleet`. The plan made the *resolver* the sole populator of that carrier (KTD2). To catch a caller that forgot to resolve, a guard was added to `run_fleet`: raise if any `spec.effective_instruction` is empty.

A cross-model (Codex) review flagged a `[high]`: the guard **broke the pre-persona engine API**. `FleetConfig.load(focus_only_fleet); run_fleet(cfg)` now raised `ValueError`, because `load()` does not resolve, and the engine no longer reads the original `spec.focus`. A focus-only fleet — a complete config that needs no file I/O — could no longer run from `load()` alone.

## Guidance

When a resolver becomes the **sole** populator of a carrier field that a pure consumer reads, populate the **cheap, no-I/O case eagerly at parse time**. Reserve the resolver for the case that genuinely needs I/O.

```python
# config.from_dict — populate the carrier for the no-I/O case at parse:
SpecialistSpec(
    ...,
    focus=focus,
    persona=persona,
    # focus is a complete, no-I/O instruction; a persona has focus="" here and
    # stays empty until the resolver reads its file.
    effective_instruction=focus,
)
```

Then: the common path (`load` -> consume) needs no resolution; the resolver only does the genuine I/O (read the persona file); and the resolve-before-use guard fires **only** on the case that actually requires resolution (an unresolved persona) — the real bug, not every direct caller.

## Why This Matters

Making resolution the *only* population point turns "call resolve() first" into a **hidden precondition** on the consumer's public API. Direct callers (`load -> run_fleet`, library users, tests) that worked before now fail, and the precondition is invisible at the call site. Same-model review *endorsed* the guard (three reviewers asked for it); cross-model review caught that its design broke the API — populate-eagerly removes the precondition for the common case while keeping the guard meaningful for the I/O case.

Note the rejected alternative: "make the consumer fall back to the original field" (`run_fleet` reads `spec.focus` when `persona` is empty) would re-introduce input-field knowledge into the pure consumer — here it would have failed the engine-purity test (`engine.py` may not reference `persona`/file fields). Populating at parse keeps the consumer reading exactly one field.

## When to Apply

Any time you introduce a resolver/normalizer that becomes the only populator of a field a downstream pure component depends on, **and** some inputs produce that field with no I/O. Push the cheap case to parse; keep the resolver for the expensive case.

## Examples

- **Before:** carrier populated only in `resolve()` → `FleetConfig.load(focus_only); run_fleet(cfg)` raises (hidden precondition).
- **After:** `from_dict` sets `effective_instruction = focus` for focus-only specs → `load -> run_fleet` works with no resolve; `resolve()` reads only persona files; the guard fires only on an unresolved persona.

## Related

- `docs/solutions/design-patterns/enumerate-consumers-when-a-new-value-aliases-a-load-bearing-state.md` — same `effective_instruction` carrier, complementary angle: that doc says *enumerate every reader* of the new carrier; this one says *populate it eagerly for the cheap case* so resolution is not a hidden precondition.
- `docs/solutions/architecture-patterns/side-effects-at-the-edge-pure-engine-core.md` — the resolver is the edge; this learning is about not making the edge a mandatory step for inputs that need no edge work.
