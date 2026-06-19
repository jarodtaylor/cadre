---
title: "Loading a by-path module that uses @dataclass needs sys.modules registration before exec_module"
date: 2026-06-19
category: conventions
module: tests
problem_type: convention
component: testing_framework
severity: medium
applies_when:
  - "Loading a non-package module by path via importlib.util.spec_from_file_location + exec_module"
  - "That module uses @dataclass together with `from __future__ import annotations`"
  - "The repo keeps runnable scripts outside the package tree (spikes/, scripts/) and tests them by path"
tags: [testing, importlib, dataclass, sys-modules, by-path-import, python, from-future-annotations]
---

# Loading a by-path module that uses @dataclass needs sys.modules registration before exec_module

## Context

Cadre keeps runnable, non-importable scripts outside the package tree — `spikes/` and `scripts/` are **not** packages — and the test suite loads them by path to unit-test their pure functions (`tests/test_palette.py` loads `spikes/verify_aiagent_providers.py`; `tests/test_install.py` loads `scripts/resolve_venv.py`; `tests/test_cli.py` loads the skill `run.py`). When such a module defines a `@dataclass` **and** has `from __future__ import annotations` (the repo-wide convention), a naïve by-path load crashes on the decorator:

```
AttributeError: 'NoneType' object has no attribute '__dict__'
  ... in dataclasses._is_type: ns = sys.modules.get(cls.__module__).__dict__
```

## Guidance

Register the module in `sys.modules` under the spec name **before** calling `exec_module`:

```python
def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # <-- before exec_module, or @dataclass crashes
    spec.loader.exec_module(mod)
    return mod
```

This is the established pattern in the repo's test loaders (`_load_spike`, `_load_skill_module`) — follow it for any new by-path-loaded module.

## Why This Matters

With `from __future__ import annotations`, every annotation is a **string**. At class-definition time `@dataclass` calls `_is_type` to detect `KW_ONLY` sentinels in those string annotations, and `_is_type` resolves the module namespace via `sys.modules.get(cls.__module__).__dict__`. If the module isn't registered, `sys.modules.get(...)` returns `None` and the decorator raises. Running the file directly (`python spikes/foo.py`) works because `__main__` is always in `sys.modules` — so the gap only appears under by-path `exec_module` loading, exactly what the tests do. Skipping the registration produces a confusing decorator-time crash that looks like a dataclass bug rather than a loader bug.

## When to Apply

- Adding a new `spikes/` or `scripts/` module that defines a dataclass and testing it by path.
- Any `spec_from_file_location` + `exec_module` load of a module using `@dataclass` with future-annotations.
- Not needed for normal package imports (`from fleet_engine... import`) — those register themselves.

## Examples

Broken — decorator crashes at load:

```python
spec = importlib.util.spec_from_file_location("verify_aiagent_providers", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)          # AttributeError in @dataclass
```

Fixed — register first:

```python
spec = importlib.util.spec_from_file_location("verify_aiagent_providers", path)
mod = importlib.util.module_from_spec(spec)
sys.modules["verify_aiagent_providers"] = mod
spec.loader.exec_module(mod)          # dataclass resolves cls.__module__ fine
```

## Related

- `docs/solutions/architecture-patterns/lazy-import-adapter-for-volatile-dependencies.md` — the broader "keep host-only / non-package code loadable and fake-testable on dev" stance these by-path loaders serve.
