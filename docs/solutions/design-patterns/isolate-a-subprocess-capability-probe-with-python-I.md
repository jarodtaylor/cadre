---
title: "Isolate a subprocess capability/integrity probe with `python -I`, not `-P` or a bare `-c`"
date: 2026-07-05
category: design-patterns
module: cadre
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "You spawn a subprocess to check a property of a SPECIFIC target interpreter (can it `import X`? does it have feature Y?)"
  - "The answer must reflect that interpreter's OWN install, not the caller's environment (cwd, PYTHONPATH, user-site)"
  - "The check is a fail-closed gate — you refuse to trust/record the interpreter if the probe fails"
---

# Isolate a subprocess capability/integrity probe with `python -I`, not `-P` or a bare `-c`

## Context

A common trust-surface gate: before recording or trusting an operator-supplied interpreter, verify it can actually `import` the package that depends on it. The naive probe is

```python
subprocess.run([python_path, "-c", "import cadre"])
```

This is **fail-OPEN**. `python -c` puts the current working directory on `sys.path[0]`, and `PYTHONPATH` is inherited from the parent — so the import can resolve from the *caller's* environment rather than the *target interpreter's own* site-packages. The gate then passes for an interpreter that can't really import the package standalone.

## Guidance

Run the probe in **isolated mode**: `python -I -c "import cadre"`.

`-I` composes three isolations that a fail-closed check needs, and neither `-P` alone nor a bare `-c` gives you all of them:

| Flag | Drops cwd/script dir | Ignores `PYTHONPATH`/`PYTHON*` env | Ignores user-site |
|---|---|---|---|
| (bare `-c`) | no | no | no |
| `-P` | yes | **no** | no |
| `-I` (= `-P` + `-E` + `-s`) | yes | yes | yes |

A pip-installed package still resolves under `-I`, because it lives in the interpreter's **own system site-packages** — which `-I` keeps — not in user-site or on `PYTHONPATH`. So `-I` narrows the probe to exactly the question you meant to ask ("can *this* interpreter import it on its own?") without any false-positive channel.

## Why This Matters

The gate exists to refuse an interpreter that cannot import the package. Any environment channel that satisfies the import for a *different* interpreter defeats it — you record a broken or attacker-influenced Python and the failure surfaces later, far from setup.

This was learned in two review passes on the same check (`cadre/provision.py` `verify_importable`):

1. First fix: `-c` → `-P`. A same-model code review caught that `install.sh` does `cd "$REPO_ROOT"` before running setup, so the in-tree `cadre/` package satisfied `import cadre` for *any* runnable interpreter — the cwd fail-open.
2. Still incomplete: a **cross-model adversarial pass** then caught that `-P` drops cwd but still honors `PYTHONPATH=/attacker` — the same fail-open through a different channel. `-I` closed it.

The lesson is to reach for `-I` up front, rather than patching one leak (`-P`) and shipping the next.

## When to Apply

Any subprocess that probes a capability or property of a **specific** target interpreter/binary where the answer must reflect that target alone — especially a fail-closed security or integrity gate. If the probe's truth could be changed by the caller's cwd, env vars, or user-site, isolate it.

## Examples

```python
# Fail-open: cwd on sys.path[0] AND inherited PYTHONPATH can both satisfy the import.
subprocess.run([python_path, "-c", "import cadre"], capture_output=True, timeout=t)

# Fail-open closed only for cwd — PYTHONPATH still bypasses.
subprocess.run([python_path, "-P", "-c", "import cadre"], ...)

# Correct: -I isolates cwd + PYTHON* env + user-site. Only the target's own
# system site-packages count.
subprocess.run([python_path, "-I", "-c", "import cadre"], ...)
```

Related:
- [Fail-closed allowlist for capability gates](fail-closed-allowlist-for-capability-gates.md) — the same fail-closed posture for *which* capabilities are allowed; this doc is the fail-closed posture for *verifying an interpreter has* one.
- [Validate and read the same fd to defang special files](validate-and-read-the-same-fd-to-defang-special-files.md) — another "harden the probe itself, not just its result" trust-surface pattern.
