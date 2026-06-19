---
title: "Side-effects at the edge: keep the engine core pure and testable"
date: 2026-06-18
category: architecture-patterns
module: fleet_engine
problem_type: architecture_pattern
component: service_object
severity: medium
applies_when:
  - Adding a capability that needs I/O (capture, logging, persistence) to a core that must stay testable without it
  - Extending the fleet engine with a new cross-cutting concern
tags: [engine-purity, separation-of-concerns, caller-layer, testability, fleet-engine]
---

# Side-effects at the edge: keep the engine core pure and testable

## Context

Cadre's engine (`fleet_engine/engine.py`, `model_client.py`) is deliberately free of file I/O and holds no fleet-domain strings, so it imports and runs against fakes with no live calls. Run capture needed to persist each run's artifacts to disk — a side effect that, added naively inside `run_fleet`, would have broken that purity and made the hermetic test suite write to the filesystem on every run.

## Guidance

Add the capability at the **edge**, not the core:

- The side effect (file I/O) lives in a **caller-layer module** (`fleet_engine/capture.py`) imported **only** by the entry points (`cli.py`, `skills/cadre-fleet/run.py`) — never by the engine. The engine *core* stays pure, not the whole package: `config.py` already reads files and `cli.py` already calls `print()` at the edge.
- The core emits only cheap **data signals**, never I/O. The engine gained `elapsed_s`, `toolset`, `timed_out` on `AgentResult` and `synth_ok` on `FleetResult`, all set at the per-lane collection site — plain fields, no file handling.
- The caller reads those signals plus the in-memory result and writes the artifacts. **The engine never sees a path.**

This is the same move as isolating a volatile dependency behind a lazy-imported adapter (see Related), generalized from "isolate the dependency" to "isolate every edge concern — dependencies *and* side-effects — and keep the core a pure data producer."

## Why This Matters

The engine stays unit-testable against fakes with zero live calls or disk writes — the property that lets the full suite run hermetically on a dev machine with no providers. A side effect in the core would couple every test to the filesystem and the runtime. It also keeps the core reusable: the same engine output now feeds capture and will feed the deferred audit and agent-handoff layers without the engine knowing they exist.

## When to Apply

- Adding I/O, persistence, logging, or any side effect to a core that must stay testable without it.
- Extending the engine with a cross-cutting concern — prefer a new caller-layer module plus new data fields on the result over reaching into the core.

## Examples

Before: capture would `open()`/`write()` inside `run_fleet`. After: `run_fleet` sets data fields; the caller calls `save_run(cfg, result, run_dir)`. Purity is verifiable mechanically — grepping `engine.py`/`model_client.py` for `open(`/`Path(`/`mkdir`/an import of `capture` returns nothing but an unrelated docstring word.

A discipline carried along: the `[]`-vs-`None` toolset rule (an empty toolset must serialize as an explicit `[]`, never `null` — the same aliasing that once caused a privilege bypass) re-applied the moment the toolset reached the JSON manifest. **Carry a hard-won representation rule to every representation of that value**, not just the one where it first bit.

## Related

- `docs/solutions/architecture-patterns/lazy-import-adapter-for-volatile-dependencies.md` — the dependency-isolation case of the same principle; this doc generalizes it to side-effects. (Moderate overlap — candidate for a future consolidation review.)
- `docs/solutions/security-issues/empty-toolset-collapsed-to-all-tools.md` — the `[]`-vs-`None` rule carried into manifest serialization.
- `docs/solutions/integration-issues/hermes-tools-are-profile-scoped.md` — why the manifest records `HERMES_HOME`: a data signal that makes profile-scoped grounding failures diagnosable after the fact.
