---
title: "Distinguish 'absent' from 'present-but-invalid' in a degrade-open gate"
date: 2026-07-06
category: design-patterns
module: "cadre preflight (#62 palette spend-gate: preview_lint.resolve_palette_path + preflight.preflight_refusal); generalizes to any fail-open gate whose config loader collapses missing and malformed to one sentinel"
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A safety, spend, or permission gate degrades OPEN (proceeds) when its config/policy source is absent"
  - "The loader returns one sentinel (None / empty / a default) for BOTH a genuinely-absent source AND a present-but-unreadable/malformed one"
  - "A corrupted or partially-written source would therefore silently disable the gate instead of surfacing an error"
tags: [trust-safety, safety-gate, fail-closed, fail-open, config-loading, defense-in-depth, cross-model-review]
---

# Distinguish 'absent' from 'present-but-invalid' in a degrade-open gate

## Context

Cadre's #62 preflight gate refuses a fleet run before any model spend when a specialist/synthesizer/judge model is off the host palette. It loads the palette with `load_palette()`, which returns `None` and never raises. The first draft treated any `None` as permission to proceed ("no palette to check against -> degrade open"), matching the preview's existing posture.

That is correct for a *genuinely-absent* palette — the operator opted out of palette checking. But `load_palette()` also returns `None` for a palette that is **present but unreadable/malformed**: bad YAML, missing keys, a partial write, a permissions error. So a corrupted palette silently turned the spend-gate into a no-op — off-palette models would run and spend, no refusal, no signal. The whole point of the gate (don't spend on a bad config) was defeated by exactly the "bad config" case.

All five same-model reviewers (correctness, security, adversarial, maintainability, testing) cleared this as "acceptable by design — degrade-open matches the preview." A cross-model adversarial pass (Codex) caught it, because it questioned the *premise* the same-model reviewers had accepted.

## Guidance

A degrade-open gate must distinguish **absent** (operator opted out -> proceed) from **present-but-invalid** (operator wants the gate but the source is broken -> **fail closed** with a clear error). Do not let one sentinel value collapse the two.

The mechanism: give the loader a companion that resolves *where the source would be* without parsing it, then stat that path when the load returns the sentinel.

```python
# preview_lint.py — resolve the path the loader would read, without parsing it
def resolve_palette_path(path=None):
    ...  # param -> env -> default; return None only if the path spec itself is unusable

# preflight.py — the gate
palette = load_palette(palette_path)
if palette is None:
    resolved = resolve_palette_path(palette_path)
    if resolved is not None and resolved.exists():
        return "<refuse: palette present but unreadable/malformed — fix or remove it>"  # fail CLOSED
    return None  # genuinely absent -> degrade OPEN (operator opted out)
```

## Why This Matters

A safety gate that a broken config silently disables is worse than no gate: it *looks* protective (green tests, a happy path that works) while providing nothing in the one state — a corrupted policy source — where protection matters most. "Fail open on absent" feels safe and is right for opt-out; the trap is reusing that branch for "invalid," where fail-*closed*-with-a-loud-error is the honest behavior. It also aligns with correctness-DevEx: a bad config should fail loudly and early, not spend and degrade.

This is a durable reason cross-model review earns its keep on safety code: same-model reviewers share the author's framing and will accept a premise ("degrade-open matches the preview") that a different model questions.

## When to Apply

- Any gate that degrades open when a config/policy/allowlist source is missing — a toolset allowlist, a feature-flag policy file, auth/permission config, a spend guard.
- Any loader whose "not usable" return conflates *missing* with *malformed* (returns `None`/`{}`/a default on both). Before wiring it into a gate, ask: "what does a corrupted source do here?" If the answer is "same as no source," split them.

## Examples

Before (the trap): `if load_config() is None: proceed()` — a corrupted config proceeds ungated.

After: split the sentinel by statting the resolved source path — absent proceeds, present-but-invalid refuses with a clear "fix or remove it" error.

Related: [[verify-the-consumer-relies-on-the-defense-property]] (a defense only helps if the consumer relies on the property it adds — here, the gate only protects if a broken source fails closed). Cross-model review caught this after five same-model reviewers cleared it — a recurring signal that a different model family questions premises a shared-model panel accepts.
