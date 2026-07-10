---
title: "A model-family rule must match aggregator vendor/model slugs and case variants"
date: 2026-07-10
category: design-patterns
module: "cadre policy gate (#78: policy.check restrict_models glob matching); generalizes to any policy/filter that matches model identifiers across providers"
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A rule matches model identifiers by pattern (glob/prefix/regex) and the same model reaches you under more than one spelling"
  - "Aggregator or gateway providers expose models as vendor/model slugs while direct providers use the bare model id"
  - "Identifier casing is host-defined and not guaranteed stable between the config author and the runtime"
symptoms:
  - "The rule visibly blocks the bare-id and wrong-provider forms, so spot checks pass — false confidence"
  - "The one route the rule was written to stop (the aggregator spelling) is silently allowed"
tags: [trust-safety, policy, model-matching, glob, aggregator-slugs, casefold, fail-closed]
---

# A model-family rule must match aggregator vendor/model slugs and case variants

## Context

Cadre's policy gate lets an operator pin a model family to approved providers with a glob: `match: "family-*"`. The same model commonly reaches a host under **two spellings**: the bare id (`family-x`) on direct providers, and an aggregator slug (`vendor/family-x`) on gateway/marketplace providers. The first implementation tested the glob against the full model string only — so `family-*` matched `family-x` but **not** `vendor/family-x`. The aggregator route is often exactly the wrong-billing-route the rule exists to stop, and the seeded config template taught the failing pattern. Verified empirically in review:

| pair checked | intended | first implementation |
|---|---|---|
| `approved-route / family-x` | allow | allow ✓ |
| `other-route / family-x` | block | block ✓ |
| `other-route / vendor/family-x` | **block** | **allow ✗** |

Two spot checks passing is what makes this dangerous — the operator sees the rule "working."

## Guidance

When matching model identifiers by pattern in a policy or filter:

1. **Test the pattern against the full id AND its post-last-`/` segment** (`model.rsplit("/", 1)[-1]`), so a family glob catches the aggregator spelling. Matching may only *widen* what gets blocked — a false block is a loud, zero-cost refusal; a false allow is the bug.
2. **Compare case-insensitively** (casefold both sides — and prefer a case-deterministic matcher like `fnmatch.fnmatchcase` over an OS-dependent one, feeding it casefolded inputs). A case-mismatched rule in a money-safety file is a **silent no-op**, the harmful direction.
3. **Say so where configs are authored**: the seed/template must state which shapes the pattern is tested against, with an aggregator-shaped example — otherwise the template itself teaches the failing pattern.

## Why This Matters

The identifier space is defined by the *hosts*, not by the config author. A rule that encodes only the spelling the author happens to know fails open on every other spelling — and the failure is invisible because the known spellings still block correctly. In a spend gate, that is precisely the silent-wrong-route spend the feature exists to prevent.

## When to Apply

- Model allow/deny policies, routing rules, cost filters — anywhere a pattern meets a provider-qualified identifier.
- Any system where an aggregator, proxy, or marketplace re-namespaces upstream identifiers.
- Less critical when identifiers come from a single closed enum you control end to end.

## Examples

```python
def _model_matches(pattern_cf: str, model: str) -> bool:
    model_cf = model.casefold()
    return (
        fnmatch.fnmatchcase(model_cf, pattern_cf)
        or fnmatch.fnmatchcase(model_cf.rsplit("/", 1)[-1], pattern_cf)
    )
```

Regression tests should pin all three truth-table rows above — the aggregator row is the one that catches the class.

## Related

- `intersect-overlapping-allow-rules-dont-first-match.md` — sibling finding: the composition half of the same gate.
- `fail-closed-allowlist-for-capability-gates.md` — the shared posture: resolve ambiguity toward blocking.
