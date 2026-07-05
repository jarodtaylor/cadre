---
title: "Probe a proxy detector against real captured output before building it"
date: 2026-07-04
category: best-practices
module: "fleet_engine iterative topology (the deferred #42 consensus auto-stop); generalizes to any feature whose payoff rests on a proxy signal tracking a hard-to-measure real property"
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - "A feature's value rests on a proxy/heuristic standing in for a real property you cannot cheaply measure directly (text-similarity for semantic convergence, a change-delta for 'materially moved', a score for relevance/quality)"
  - "The detector would gate behavior (stop early, skip, escalate) on that proxy crossing a threshold"
  - "Real output the proxy would run against is already captured somewhere (a run folder, a log, a transcript) so a probe is free or near-free"
tags: [validate-first, proxy-signal, detector, heuristic, dogfood, convergence, iterative, requirements-gate]
---

# Probe a proxy detector against real captured output before building it

## Context

GH #42 proposed an opt-in consensus auto-stop for the `iterative` topology: end a debate's rounds early once the lanes "converge," detected by a pure round-over-round text-similarity heuristic (`stop_when: stable`). The design was deliberately minimal and trustworthy — a computable, auditable signal, no model call. Its entire value rested on one unproven assumption: that round-over-round text-similarity tracks whether a lane's *position* actually stopped moving.

Rather than build it, a validate-first probe (the same gate #18 used before committing to iterative) tested that assumption against real output — for free. The existing #18 debate transcript (proponent / skeptic / contrarian, 3 rounds) was already on the host, so the probe replayed it offline with zero paid calls: per-lane round-over-round `difflib.SequenceMatcher` ratio, swept across thresholds.

The result falsified the premise. Similarity was **0.03–0.13** — the lanes rewrite ~87–97% of their text every round. An all-lanes-stable trigger never fires at any threshold from 0.95 down to 0.50; single-round and 2-consecutive both run to the cap. The detector would save **zero** rounds on the flagship fleet. #42 was deferred on that evidence, before a line of feature code was written.

## Guidance

When a feature's payoff depends on a **proxy signal** tracking a **real property you can't cheaply measure directly**, validate the proxy against real captured output with a cheap offline probe *before* building the detector. Do not reason about whether the proxy should work — measure whether it does, on the actual output it will run against.

The move, concretely:

1. **Find real captured output the proxy would score.** A prior run's artifacts (run folder, transcript, log) are ideal — replaying them costs nothing and the data is honest. Cadre's on-by-default run capture made the #42 probe free.
2. **Compute the proxy exactly as the feature would.** Use the same instrument the spec names (here, stdlib `difflib` on raw text — no smarter metric, or you validate a detector you're not building).
3. **Ask two questions.** Would the detector *ever fire* usefully (sweep the threshold, don't guess one)? And when it fires, does it fire on the *real property* or an artifact? A negative on either kills the design for the cost of a script.

A negative result is a *win*: it retires a plausible-but-broken feature at requirements time instead of after a build cycle. Bank the probe as durable evidence on the issue.

## Why This Matters

The proxy was inert for a structural reason the build would have shipped blind. Iterative rethreads every sibling's output into each lane's prompt each round, so a lane substantially **rewrites its surface even when its position has converged** — raw text-similarity measures surface churn, not position stability. A detector that could actually see convergence needs semantic comparison (a model call), which the design had explicitly rejected for auditability and cost. So the cheap-and-trustworthy version *cannot* work and the version that might was *already ruled out* — a contradiction worth discovering at requirements time, not in a post-mortem.

This is the same deep failure as trusting any proxy that can silently diverge from the thing it stands for (see the sibling learning on tool-use detection). The addition here is method: a proxy detector is cheap to *falsify empirically* against captured output, so falsify it first. The review lenses (product-lens, adversarial) *predicted* this analytically; the probe made it a measured fact — and a measured fact is what closes a design question, where a predicted risk only opens one.

## When to Apply

Any feature whose value hinges on a proxy or heuristic tracking a real property you cannot measure directly and cheaply: early-termination / auto-stop detectors, convergence or agreement signals, "did it change materially" gates, relevance / novelty / quality heuristics, dedup-by-similarity. The probe is most worth it — and usually free — when real output is already captured so replay costs nothing.

Skip when the proxy *is* the property (no gap to validate — an exact-match check on an ID), or when the proxy is already validated, or when standing up a probe would genuinely cost more than just building the thing behind a flag and measuring in place.

## Examples

**#42 text-stability (this repo).** Offline replay of the #18 debate transcript: per-lane round-over-round `difflib` similarity 0.03–0.13; all-lanes-stable never fires across thresholds 0.95→0.50; detector saves 0 rounds → deferred. Root cause: cross-round threading forces heavy rewriting, so text-similarity can't see semantic convergence. Cost of the probe: one script, zero paid calls. Cost avoided: a full build of an inert feature plus the manifest/preview/consumer surface it would have added to a solo-maintained tool.

**General shape.** A "skip re-embedding when the document hasn't materially changed" cache keyed on a cheap text-diff: before building, run the diff over a week of real edits and check whether it actually separates trivial edits from material ones, or whether formatting churn (re-exports, whitespace, reordering) trips it on unchanged content. If the diff can't tell them apart on real data, the cache is a correctness hazard, not a speedup — learned from a replay, not from production.

## Related

- `verify-tool-use-by-effect-not-dispatch-signal.md` — the sibling in the "a proxy signal can lie" family. There a dispatch-side signal (a round-trip count) fails to see a server-side native tool fire; here a text-similarity signal fails to see semantic convergence. That one's remedy is reading the source for the detection ceiling; this one's is replaying real output before building the detector.
- `prove-a-threaded-primitive-actually-threads.md` — the #18 validate-against-real-behavior methodology this extends. That proved a primitive *does* the thing (round 2+ cites sibling-only evidence); this proves a proposed detector *can't* — same gate, opposite verdict, both cheaper than shipping and finding out.
