---
title: Expand ~ before realpath when confining a caller- or env-supplied path
date: 2026-06-24
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A path-confinement reads a directory from a default constant or an environment variable"
  - "That directory may be written ~-relative (e.g. ~/.cadre/personas)"
  - "You canonicalize it with os.path.realpath before confining or opening files under it"
tags: [path-confinement, realpath, expanduser, tilde, posix-footgun, silent-failure, security]
---

# Expand `~` before realpath when confining a caller- or env-supplied path

## Context

The persona resolver (`fleet_engine/personas.py:resolve`) confines a pool directory — `~/.cadre/personas` by default, overridable via `CADRE_PERSONAS_DIR` — and canonicalizes it with `os.path.realpath` before the realpath-confine + `O_NOFOLLOW` open. The full unit suite was green (650+ tests), yet the flagship feature could not run once: `validate fleets/doc-review.example.yaml` raised

```
persona pool directory not found or not a directory: '~/.cadre/personas'
```

— note the **literal `~`**. Install seeds the *expanded* `/Users/<you>/.cadre/personas`; the resolver was looking somewhere else entirely.

## Guidance

`os.path.realpath` does **not** expand `~` — only `os.path.expanduser` does. When a path may be `~`-relative (a default constant, an env var, user config), call `expanduser` **before** `realpath`:

```python
# WRONG — realpath leaves the leading ~ as a literal path segment
pool_real = os.path.realpath(pool_dir)            # "~/.cadre/personas" -> "<cwd>/~/.cadre/personas"

# RIGHT — expanduser first, then canonicalize
pool_dir = os.path.expanduser(os.fspath(pool_dir))  # "~/.cadre/personas" -> "/Users/you/.cadre/personas"
pool_real = os.path.realpath(pool_dir)
```

For a confinement, do the `expanduser` **first in the chain**, before the realpath-confine and the `O_NOFOLLOW` open — it only expands `~`, so it does not weaken the confinement ordering.

## Why This Matters

`realpath("~/.cadre/personas")` treats `~` as an ordinary directory name and returns `<cwd>/~/.cadre/personas`, which never matches the install-seeded directory. The failure is **silent and total**: every run of a fleet that references the pool raises, even though the install put the files in the right (expanded) place. The two halves — the writer (`expanduser` at install via `Path(...).expanduser()`) and the reader (`realpath` without `expanduser`) — disagreed about where the pool lived.

The unit suite was **blind** to it because every test passed an already-absolute `pool_dir` (`realpath(tmp)`, `_REPO/"personas"`, `/unused`). The `~`-default code path was never executed by a test. This is why it took *running the feature once for real* (a dogfood `validate`) to surface it — a green pure-test suite is not evidence the feature runs.

## When to Apply

Any path-confinement or file-open that accepts a directory from a default, an environment variable, or user config that could be written `~`-relative. The same trap applies to a `~`-containing env override, not just the default constant.

## Examples

The regression guard must use a `~`-relative path, since an absolute one masks the bug:

```python
def test_tilde_pool_dir_is_expanded(self):
    with tempfile.TemporaryDirectory() as home:
        home = os.path.realpath(home)
        pool = os.path.join(home, ".cadre", "personas")
        os.makedirs(pool)
        open(os.path.join(pool, "p.md"), "w").write("body\n")
        cfg = _make_collect_config([_persona_spec("p")])
        with patch.dict(os.environ, {"HOME": home}):
            resolve(cfg, "~/.cadre/personas")   # must expand ~ -> <home>/.cadre/personas
        self.assertEqual(cfg.specialists[0].effective_instruction, "body\n")
```

## Related

- `docs/solutions/design-patterns/atomic-directory-reservation-over-check-then-create.md` — the seeding side that wrote the *expanded* path the reader failed to match.
- The broader lesson: a pure-test suite can be 100% green while the feature has never run once — dogfood the real entry point (see the memory note *dogfood-before-agent-handoff*). Surfaced by an advisor "run it once for real" before declaring done.
