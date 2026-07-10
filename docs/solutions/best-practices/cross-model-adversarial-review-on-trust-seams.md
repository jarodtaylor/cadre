---
title: "Run a cross-model adversarial pass on trust-seam changes — same-model review layers share blind spots"
date: 2026-07-10
category: best-practices
module: "review pipeline for engine/trust-surface changes; evidence from the #62 palette gate and the #76/#78 adapter + policy-gate wave"
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - "A change touches a trust seam: a spend/permission gate, a result-classification boundary, an admission filter, a sanitization chokepoint"
  - "The same-model review pipeline (multi-lens, multi-persona) has already run and come back clean"
  - "The cost of a wrong premise is silent (spend, privilege, false success) rather than loud (crash, red test)"
tags: [code-review, cross-model, adversarial-review, trust-safety, review-pipeline, model-diversity]
---

# Run a cross-model adversarial pass on trust-seam changes — same-model review layers share blind spots

## Context

Two separate incidents in this repo, three weeks apart, with the same shape:

- **#62 palette gate (2026-07-06).** Five same-model review lenses (correctness, security, adversarial, maintainability, testing) all cleared a degrade-open loader as "acceptable by design." A cross-model adversarial pass caught that it collapsed *absent* with *present-but-invalid* — a corrupted config silently disabled the spend gate (recorded in `distinguish-absent-from-invalid-in-a-degrade-open-gate.md`).
- **#76/#78 wave (2026-07-10).** A policy gate and a result-classification adapter went through an unusually deep same-model pipeline — plan review, three simplify lenses, correctness + adversarial + invariant reviewers, plus two independent review bots — which folded 10+ real findings. A cross-model adversarial pass **still** returned 4-for-4 real findings with zero noise, including two high-severity money-path semantic gaps: first-match-wins rule composition failing silent-open on ordering, and explicit-override absence degrading permissive. Every one was a **premise** question, not an implementation bug.

The pattern: same-model reviewers — however many lenses, personas, or passes — tend to accept the same *design premises* the author held, because they reason from shared priors. The findings that survive a deep same-model pipeline are disproportionately premise-level: "is first-match-wins the right composition?", "is absent really opt-out here?", "does the receipt promise hold on the expensive failure path?"

## Guidance

For any trust-seam change, add **one cross-model adversarial pass after the same-model pipeline is clean**, and frame it as a challenge review — question the chosen semantics, the assumptions, and where the design fails under real conditions — not as a stricter defect lint.

Triage its output as **defect vs deferred-scope** before acting: cross-model passes grade against an ideal and can bundle real defects with out-of-scope ambitions (see `confirm-pass-a-folded-adversarial-no-ship.md`). In the #76/#78 wave the split was 4 defects / 0 deferred; in earlier rounds it has skewed the other way — the triage step is what keeps the practice cheap.

Sequencing matters: run it **last**. The same-model pipeline strips implementation-level findings first, so the cross-model pass spends its attention where it is uniquely strong — the premises.

## Why This Matters

Lens decomposition is the primary breadth lever and one strong model exercises it fully (`lens-decomposition-vs-model-diversity-in-review-fleets.md` — the product-side experiment found the same increment: diversity surfaces what every lane of a single model misses). But breadth within one model cannot manufacture independence: agreement among same-model lenses is not independent confirmation. On a trust seam, the expensive bugs are exactly the shared-premise kind — silent-open composition, silent-permissive absence, silent-null receipts — where every same-model reviewer nods for the same reason the author did.

## When to Apply

- Changes to gates (spend, permission, admission), classification boundaries (success/failure semantics), sanitization chokepoints, or money-path matching logic.
- After the same-model pipeline is clean — not as a replacement for it.
- Skip for mechanical changes, docs, or code with loud failure modes; the pass earns its cost where wrongness is silent.

## Examples

From the #78 pass — the finding no same-model layer raised, verbatim premise challenge: *"overlapping model restrictions allow the first broad route and ignore stricter rules … this plausible broad-rule-plus-exception configuration silently defeats the money-safety invariant at all three chokepoints."* The fix (intersection composition) is recorded in `intersect-overlapping-allow-rules-dont-first-match.md`.

## Related

- `distinguish-absent-from-invalid-in-a-degrade-open-gate.md` — incident one, including the five-lenses-cleared-it record.
- `intersect-overlapping-allow-rules-dont-first-match.md`, `split-absent-default-from-absent-explicit-override.md` — incident two's premise-level findings.
- `lens-decomposition-vs-model-diversity-in-review-fleets.md` — the product-side evidence for the same mechanism.
- `confirm-pass-a-folded-adversarial-no-ship.md` — the triage discipline that keeps adversarial passes actionable.
