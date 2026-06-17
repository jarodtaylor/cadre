---
title: Gate an externally-controlled capability vocabulary with a fail-closed allowlist
date: 2026-06-17
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "Gating which capabilities or permissions a config may grant"
  - "The set of capability names comes from an external dependency you do not control"
  - "That set can grow, be renamed, or include composite entries across versions"
  - "The gated actor processes untrusted input"
tags: [security, allowlist, fail-closed, trust-boundary, authorization, config-validation]
---

# Gate an externally-controlled capability vocabulary with a fail-closed allowlist

## Context

A fleet specialist may be granted Hermes "toolsets" (`web`, `x_search`, `file`, `terminal`, `browser`, ...). Some act beyond reading (shell, file-write, code execution, browser control), and a specialist ingests untrusted web content — so privileged toolsets sit behind an explicit `allow_privileged_tools` opt-in. The gate was first written as a **denylist** of privileged names: `{terminal, code_execution, file, computer_use}`.

## Guidance

When you gate which entries of a vocabulary are allowed, and that vocabulary is **defined by an external dependency you don't control** — it can grow, be renamed, or add composite entries across versions — gate with a **fail-closed allowlist** of known-safe entries, not a denylist of known-dangerous ones. Anything not on the safe list (privileged, composite, *or unrecognized*) is rejected unless explicitly opted in.

A denylist fails **open**: every entry the dependency adds or renames is permitted until a human notices. A cross-model review found exactly this — the denylist missed `browser` (real, privileged) and `debugging` (a composite that expands to `web + file + terminal`, so it slipped a literal-name match). An allowlist fails **closed**: a new, composite, or misspelled name errors loudly instead of leaking capability.

## Why This Matters

This is a trust boundary against untrusted-content → privileged-action escalation. Fail-open on an *evolving* vocabulary means the boundary silently weakens with every upstream release — the worst kind of regression, because nothing breaks. Fail-closed turns the same events (new name, typo, composite) into a loud validation error.

Bonus: fail-closed also catches typos. Under the denylist, a misspelled toolset (`code` — not a real Hermes name) passed validation and silently granted nothing; under the allowlist it errors, surfacing the mistake.

## When to Apply
- Gating capabilities/permissions whose names come from an external, versioned source.
- The gated actor handles untrusted input.
- The vocabulary includes composite/bundle entries that expand to others.

## Examples

Before — denylist, fails open:

```python
PRIVILEGED_TOOLSETS = frozenset({"terminal", "code_execution", "file", "computer_use"})
unsafe = set(toolset) & PRIVILEGED_TOOLSETS    # misses `browser`, `debugging`, anything new
```

After — allowlist, fails closed:

```python
SAFE_TOOLSETS = frozenset({"web", "search", "x_search", "vision", "video",
                           "image_gen", "video_gen", "tts", "moa", "todo", "clarify", "safe"})
unsafe = set(toolset) - SAFE_TOOLSETS          # privileged, composite, OR unknown -> needs opt-in
```

Curate the allowlist by **principle, not enumeration**: *safe = reads/searches/reasons/generates content, takes no action on external systems or the local machine.* Err small — the opt-in covers anything you under-include, and an unknown name erroring is a feature.

## Related
- [Empty toolset collapsed to ALL tools](../security-issues/empty-toolset-collapsed-to-all-tools.md) — the allowlist is correct, but a separate adapter bug overrode it at runtime; both layers must hold.
- Implemented as `SAFE_TOOLSETS` in `fleet_engine/config.py`; regression tests in `tests/test_config.py`.
