---
title: "Atomic directory reservation beats check-then-create"
date: 2026-06-18
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: medium
applies_when:
  - Generating a unique on-disk directory or file per run or job
  - Any check-then-act on the filesystem where two processes could race
tags: [toctou, race-condition, filesystem, atomic-reservation, run-capture, cross-model-review]
---

# Atomic directory reservation beats check-then-create

## Context

Run capture derives a per-run directory `~/.cadre/runs/<timestamp>-<slug>`. The first implementation handled same-second name collisions by checking `candidate.exists()` and then creating with `mkdir(exist_ok=True)`. The all-Claude review chain (advisor plus correctness/security/adversarial personas) passed it; a Codex cross-model adversarial pass flagged it.

## Guidance

Reserve the directory **atomically** — let the create *be* the check:

```python
while True:
    try:
        candidate.mkdir(mode=0o700, exist_ok=False)
        break
    except FileExistsError:
        candidate = base.parent / f"{base.name}-{n}"
        n += 1
```

The process that creates the directory owns it. `exists()`-then-`mkdir(exist_ok=True)` has a TOCTOU window: two processes in the same second both observe the path as free and both write into one directory, silently mixing artifacts while still presenting a "complete" run.

Pair it with a **writability probe before the expensive work**: an existing-but-unwritable target passes `mkdir(exist_ok=True)` silently, so create and delete a sentinel file up front to fail fast — never spend a multi-model run you cannot save.

## Why This Matters

For an audit feature, silently mixing or losing run artifacts defeats the entire purpose ("you can't trust a run you can't see"). The race is low-probability for a single user but real, and the atomic version is both cleaner and correct — there is no reason to keep the check-then-create form.

The meta-lesson: a **cross-model review earns its keep on exactly this class of bug**. Codex (GPT-5.5) found three silent-data-loss gaps — this TOCTOU, the unwritable-dir fail-fast bypass, and a sanitized-filename collision — that an advisor pass plus three Claude review personas all missed. Same-model reviewers share blind spots; a different model is a genuinely independent second opinion.

Scope it right: `/codex:adversarial-review` defaults to the **working tree** — pass `--base main` to review the committed changes on a feature branch, or it reviews almost nothing.

## When to Apply

- Any per-run or per-job unique directory/file creation.
- Any filesystem check-then-act where concurrency is possible.
- Reach for a cross-model review pass on foundational code whose failure mode is silent.

## Related

- `docs/solutions/design-patterns/daemon-threads-for-uncancellable-timeouts.md` — another concurrency-correctness learning in the same engine.
- `docs/solutions/architecture-patterns/side-effects-at-the-edge-pure-engine-core.md` — the capture architecture this directory reservation lives in.
