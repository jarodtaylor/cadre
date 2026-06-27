---
title: "Lens-decomposition drives review breadth; model-diversity adds coverage, consensus, and resilience"
date: 2026-06-26
category: best-practices
module: fleets
problem_type: best_practice
component: review_fleet
symptoms:
  - "Unsure whether a multi-model review fleet earns its extra cost vs. one strong model"
  - "A single omnibus 'review everything' prompt misses whole dimensions (e.g. architecture) a focused prompt would catch"
  - "A code-review lane confidently reports a bug that does not exist (e.g. a phantom 'missing import')"
root_cause: methodology
resolution_type: methodology
severity: medium
tags: [fleets, code-review, collect, model-diversity, lenses, evaluation, why-cadre]
---

# Lens-decomposition drives review breadth; model-diversity adds coverage, consensus, and resilience

## Context

When you point a Cadre review fleet (collect convergence) at code or a plan, two things vary at once: **how many lenses** you decompose the review into (one focus/persona per lane) and **how many distinct models** run those lenses. It's easy to credit the result to "diverse models" when most of it actually comes from the decomposition. A controlled host dogfood (2026-06-26) separated the two.

## The experiment

Same input (a small module with five planted bugs across lenses), same four lens-focuses (security / architecture / performance / correctness), varying only the model assignment:

| configuration | planted-bug catch | a cross-function defect only one model found\* | whole architecture dimension | false positives |
|---|:---:|:---:|:---:|:---:|
| **diverse models × 4 lenses** | 5/5 | ✅ found | ✅ | 0 |
| **one strong model × 4 lenses** | 5/5 | ❌ missed (all 4 lanes) | ✅ | 0 |
| **one strong model × 1 omnibus prompt** | 5/5 | ❌ missed | ❌ (per-function only) | 0 |

\* The defect: one function returned a tuple while sibling functions expected dicts, so composing them raises `TypeError`. Across all nine lanes run on the file, only a single (different) model surfaced it.

## What it shows

- **Lens-decomposition is the primary breadth lever — and needs only one model.** Going from one omnibus prompt to four focused lenses (same model) unlocked the *entire* architecture dimension the omnibus review missed (cohesion, coupling, misplaced concern, naming/contract mismatch). Decomposition, not model count, produced the bulk of the extra findings.
- **Model-diversity adds a real but smaller increment.** Holding lenses fixed, swapping in diverse models surfaced one genuine defect that five passes of a single strong model all missed — different models have different blind spots. This is *on top of* model-diversity's other two benefits:
  - **Consensus-confidence:** when independent models agree on a finding, trust it more (same-model agreement is not independent).
  - **Resilience:** a dead/unauthed provider degrades to one failed lane, not a failed review.
- **A strong single model ties on catch-rate and per-finding depth.** Diversity's edge is *coverage and trust*, not "smarter per lane."

## Practices

1. **Decompose first.** Prefer N focused lenses (distinct `focus`/`persona` per lane) over one "review everything" prompt — this is where breadth comes from, and it works even on a single model.
2. **Diversify models for coverage + consensus + resilience**, not as the main breadth driver. Treat cross-model agreement as a confidence signal.
3. **Feed full files, not a raw `git diff`.** A reviewer that sees only diff hunks confidently asserts absence-bugs ("X isn't imported / defined / guarded") for things that live outside the hunks. In the same dogfood, a model false-positived a `NameError` on a raw diff but was accurate with zero false positives on the full file. If only a diff is available, instruct lanes to flag "depends on out-of-hunk context — verify" instead of asserting.
4. **Adversarially verify confident findings before acting.** The most specific, confident finding is not the most likely to be true; verify against the exact reviewed revision.

## Honest caveat

This was a single artifact (n=1) with one planted bug-set — directional, not a benchmark. Re-run across more artifacts to *size* the model-diversity increment before leaning hard on it in claims.

## The one-line "why Cadre"

> **Decomposed lenses, cross-checked across diverse models** — lead with decomposition (the breadth lever); model-diversity buys coverage, consensus-confidence, and resilience.
