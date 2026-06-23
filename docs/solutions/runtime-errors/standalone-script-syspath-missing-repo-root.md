---
title: "Standalone entry-point script can't import the repo package — sys.path[0] is the script's dir, not the repo root"
date: "2026-06-22"
category: "docs/solutions/runtime-errors/"
module: "skills/cadre-fleet/run.py (standalone entry-point)"
problem_type: "runtime_error"
component: "tooling"
symptoms:
  - "`ModuleNotFoundError: No module named 'fleet_engine'` when running skills/cadre-fleet/run.py directly on the host"
  - "The test suite stays green — it runs under `python -m unittest` from the repo root, which masks the missing path"
root_cause: "incomplete_setup"
resolution_type: "code_fix"
severity: "medium"
tags:
  - "sys-path"
  - "standalone-script"
  - "module-import"
  - "entry-point"
  - "dogfood-finding"
---

# Standalone entry-point script can't import the repo package — `sys.path[0]` is the script's dir, not the repo root

## Problem

A standalone entry-point script (`skills/cadre-fleet/run.py`) that imports a sibling repo package (`fleet_engine/`) fails with `ModuleNotFoundError` when run directly — because `python <script>.py` puts the script's own directory on `sys.path[0]`, not the repo root where the package lives.

## Symptoms

- `ModuleNotFoundError: No module named 'fleet_engine'` when an agent or operator runs `run.py` by path on the Hermes host (`python run.py` / `$PYBIN run.py`).
- The full test suite is green at the same time — so nothing flags the break until a live, standalone invocation.

## What Didn't Work

- Relying on the test suite to catch it. The suite runs `python -m unittest discover` from the repo root, and `python -m <module>` puts the cwd (the repo root) on `sys.path[0]`, so `fleet_engine` imports fine under tests. The script's by-path invocation has different path semantics, so green tests said nothing about the standalone path.

## Solution

Have the script put the repo root on `sys.path` itself, before importing the package:

```python
# skills/cadre-fleet/run.py
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]   # run.py -> cadre-fleet -> skills -> repo root
sys.path.insert(0, str(_REPO_ROOT))

from fleet_engine.engine import run_fleet  # noqa: E402  (import after the sys.path setup)
```

Fixed in commit `a0471fc`.

## Why This Works

`python <script>.py` and `python -m <module>` initialize `sys.path[0]` differently: the former to the script file's directory, the latter to the current working directory. A script two levels deep importing a top-level package therefore works under `-m` (cwd = repo root) but not when invoked by path (`sys.path[0]` = the script's dir). Inserting the resolved repo root onto `sys.path` makes the package importable regardless of how the script is launched.

## Prevention

- Any standalone entry-point script that imports a repo package must put the repo root on `sys.path` itself — never assume the harness's cwd-on-path. Compute it from `__file__` (`Path(__file__).resolve().parents[N]`); don't hard-code the path.
- A green test suite is not evidence the script runs standalone: `python -m` masks this whole class of bug, and a by-path test loader masks it too (it also manipulates `sys.path`). Only a real standalone invocation — or a live run — exercises the script's own path setup. Add a subprocess smoke test (`python skills/.../run.py --preview`) run from the repo root, or rely on a dogfood run. This one surfaced live on the host, not in the suite ([[dogfood-before-agent-handoff]]).

## Related Issues

- `docs/solutions/conventions/by-path-module-load-register-in-sys-modules.md` — same non-package-code / `sys.path` family, a different mechanism: that doc is about by-path module *loading* registering in `sys.modules`; this is about a standalone script's `sys.path[0]` not being the repo root.
