---
title: "Force ambient host state in tests when production adds a read of it"
date: 2026-07-06
category: best-practices
module: "cadre test suite (tests/__init__.py CADRE_PALETTE pin) after #62 preflight added a read of the host palette; generalizes to any suite whose product code reads env vars, dotfiles, or host config"
problem_type: best_practice
component: testing
severity: high
applies_when:
  - "A change adds production code that reads ambient host state — an env var, a ~/. dotfile, host config, or a well-known path"
  - "The test suite exercises that code path without pinning that ambient state to a known value"
  - "CI runs in a clean container (no dotfiles, no env) so the suite stays green there regardless"
tags: [testing, hermeticity, test-isolation, ci, ambient-state, developer-experience]
---

# Force ambient host state in tests when production adds a read of it

## Context

Cadre's #62 preflight gate added a production read of the host palette (`CADRE_PALETTE` env -> `~/.cadre/palette.yaml`). The suite exercised the runners that call it but never pinned that ambient state. It stayed green on CI and on the dev laptop (neither has a `~/.cadre/palette.yaml`), so nothing looked wrong.

But on a **provisioned host** — exactly the dogfood machine where the operator has run `cadre setup` / `cadre verify-palette` and a real palette exists — the suite broke: 68 `test_cli` failures, because the example fleets' placeholder models are off that real palette, so the new gate refused them. The break was invisible to CI (a clean container) and would surface only on the machine the project actually dogfoods on. Caught by the testing lens in code review, reproduced with a poison palette.

## Guidance

When a change adds production code that reads **ambient host state**, force that state to a known value **suite-wide**, at the suite root, so the suite is hermetic regardless of the host. Tests that specifically exercise the new read override the pinned value locally (scoped, and restored to the pinned default).

```python
# tests/__init__.py — runs once when the tests package is imported by `unittest discover`
import os
# Pin the palette to a guaranteed-absent path so the #62 preflight gate sees no
# palette (degrade open) regardless of the host's ~/.cadre. Tests that exercise
# the gate set CADRE_PALETTE explicitly via patch.dict.
os.environ["CADRE_PALETTE"] = os.path.join(
    os.path.dirname(__file__), "__nonexistent__", "palette.yaml"
)
```

Two gotchas this run hit:

- **Prove the pin actually loads under the *documented* test command.** `unittest discover -s tests` runs `tests/__init__.py` only because `tests` is a package — verify it empirically with a poison value (`CADRE_PALETTE=<a real off-palette file> python -m unittest discover -s tests` must stay green), don't assume it runs.
- **Don't build the pinned path from `tempfile.gettempdir()` at import time.** It can raise in a constrained sandbox with no usable temp dir and fail the whole suite to *import*. A `__file__`-relative path has no such dependency.

## Why This Matters

CI's clean-container hermeticity is a false comfort: it hides a non-hermetic read behind a green check, and the break surfaces on the human's provisioned machine — the worst place for a suite to suddenly fail, because it reads as "your machine is broken," not "the tests aren't isolated." Pinning the ambient state at the suite root makes the suite hermetic by construction and protects every current *and future* test that hits the read, not just the ones the author remembered to patch.

## When to Apply

- Any change that adds a read of an env var, a `~/.`-dotfile, `/etc` config, or a well-known host path to product code the suite exercises.
- Especially when the read behaves differently present vs absent (a gate, a feature flag, a credential lookup) — the provisioned-host case is exactly what CI can't see.

## Examples

The tell: "green on CI and on my laptop, but I only tested where the ambient state happens to be absent." The fix is a suite-root pin, verified with a poison value — not a per-test patch you have to remember to add to every new test that touches the read.
