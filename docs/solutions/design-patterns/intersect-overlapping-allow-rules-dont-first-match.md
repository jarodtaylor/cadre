---
title: "Intersect overlapping allow-rules — first-match-wins fails silent-open on ordering"
date: 2026-07-10
category: design-patterns
module: "cadre policy gate (#78: policy.check restrict_models composition); generalizes to any allow/deny rule list where multiple rules can match one subject"
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A safety/spend/permission gate evaluates an ordered list of restriction rules and more than one rule can match the same subject"
  - "Rules pair a match pattern with an allowed set (allowlist-per-pattern), so composition semantics decide what a multi-matched subject may do"
  - "A misordered config must not silently widen what the gate permits"
tags: [trust-safety, policy, allowlist, rule-composition, fail-closed, cross-model-review]
---

# Intersect overlapping allow-rules — first-match-wins fails silent-open on ordering

## Context

Cadre's policy gate (#78) lets an operator restrict a model family to specific providers: `restrict_models: [{match, allowed_providers}]`. The first implementation used **first-match-wins** rule composition — deliberate, documented in the docstring, and pinned by tests. Every same-model review lens passed it.

A cross-model adversarial pass built the counterexample: a broad rule followed by a stricter one —

```yaml
restrict_models:
  - match: "family-*"          # broad: the family runs on the cheap route
    allowed_providers: [cheap-route]
  - match: "family-premium"    # stricter: premium ONLY via the approved route
    allowed_providers: [approved-route]
```

Under first-match-wins, `cheap-route / family-premium` matches the broad rule first and is **allowed** — the operator's stricter rule silently never applies, and the intended `approved-route / family-premium` is *blocked*. A plausible broad-rule-plus-exception config defeats the gate at every chokepoint that shares the matcher, with zero signal.

## Guidance

When multiple restriction rules match one subject, require the subject to satisfy **every** matching rule — the **intersection** of their allowed sets — instead of the first match:

- A model matching N rules is allowed only with a provider present in all N `allowed_providers` lists (compare case-insensitively).
- Rule **order stops mattering** — pin that with tests in both orders.
- The violation message names **every** matching rule, so the operator sees the whole composition that blocked the pair, not one arbitrary rule.

The asymmetry that makes intersection right for a safety gate: it can only **widen blocking**. A false block is a loud, zero-cost refusal the operator fixes in the config; a false allow is silent spend down the wrong route. Composition ambiguity must always resolve toward the loud, recoverable failure.

## Why This Matters

Ordering mistakes are operator-invisible: the config *reads* correctly ("broad default, then the exception"), each rule works in isolation, and the gate demonstrably blocks *something* — so spot checks pass while the one route the exception exists to stop sails through. Documenting "first-match-wins" does not defend against this: disclosure-only defenses protect interactive readers, not configs written months later (see `make-a-narrowing-default-non-destructive-dont-just-disclose-it.md` for the same principle on defaults).

## When to Apply

- Any rule list where patterns can overlap (globs, prefixes, regexes) and each rule carries an allow set.
- Especially when the gate guards spend, privilege, or egress — anywhere silent-open is the expensive direction.
- Not needed when rules are structurally disjoint (e.g. keyed by exact id, one rule per key) — there is no composition to decide.

## Examples

After the change, with the config above: `approved-route / family-premium` is blocked too (intersection of `[cheap-route]` and `[approved-route]` is empty) — **loudly**, with both rules named in the violation. That surfaces the operator's real intent conflict immediately, instead of silently honoring half of it; they fix the broad rule (e.g. `match: "family-standard-*"`) and the config then says what it means, in any order.

## Related

- `fail-closed-allowlist-for-capability-gates.md` — the same fail-closed posture, one level down (set membership rather than rule composition).
- `make-a-narrowing-default-non-destructive-dont-just-disclose-it.md` — why "it's documented" is not a defense.
- `split-absent-default-from-absent-explicit-override.md` — sibling finding from the same review pass, on the gate's absence semantics.
- `cross-model-adversarial-review-on-trust-seams.md` — how this class of premise-level defect gets caught.
