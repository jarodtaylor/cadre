---
title: "Folding an adversarial NO-SHIP's findings addresses them; a confirm pass is what clears the verdict"
date: 2026-07-05
category: best-practices
module: code-review
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - "A cross-model / adversarial reviewer returned NO-SHIP (or a set of findings) and you have folded fixes for all of them"
  - "The folds touch a trust surface or otherwise security-sensitive code"
  - "You are about to declare the change ready to merge on the strength of a green test suite"
---

# Folding an adversarial NO-SHIP's findings addresses them; a confirm pass is what clears the verdict

## Context

A cross-model adversarial reviewer (e.g. a Codex pass alongside same-model review) returns NO-SHIP with N findings. You fold — fix — all N, and the suite is green. Is the change now shippable?

## Guidance

**Folding the findings *addresses* them; it does not *clear* the verdict.** Run exactly **one** confirm pass before calling it done, then stop.

Two reasons the green suite is not enough:

1. **The fold code is itself new, unreviewed trust-surface code.** The reviewer never saw it. A green suite proves the fixes do what you intended and don't break *existing* tests — it says nothing about consequences the tests don't cover.
2. **A fix can spawn a consequence the finding never named.** Hardening one path changes behavior on another.

The confirm pass asks the reviewer two things about the *current* state (hand it the branch, or just the fold diffs): **(a)** are the original findings closed? **(b)** did the folds introduce anything new?

**Guardrail — one pass, not a loop.** If the confirm surfaces another genuine HIGH, fold it *only* if the fix is cheap and closed-form; otherwise timebox and surface it to the human. Do not spiral into a round-3 re-review. A confirm pass is cheaper the second time (reuse the runtime/session), so the cost is low and the signal is high.

## Why This Matters

On the cadre packaging change, folding a Codex 6-finding NO-SHIP:

- One fix (adding `O_NOFOLLOW` to two writers) **spawned an unrequested 7th consequence**: a `main()` that only caught `ValueError` now leaked the new `OSError` as a raw traceback. Nothing asked for that; the hardening created it.
- The confirm pass then verified **4 of 6 fully closed but caught 2 folds as *incomplete*** — a relative path *with* a directory component (`.venv/bin/python`) was still recorded relative (the `shutil.which` resolution didn't make it absolute), and a copy-install landed-check used `.exists()`, which **follows a symlink** and so read a preserved stale symlink as success.

Without the confirm, all three would have shipped under the banner "NO-SHIP addressed."

## When to Apply

After folding a cross-model or adversarial NO-SHIP whose findings touch a trust surface or security-sensitive code. The signal is the **fold's size and surface sensitivity**, not the finding count: a large fold on a security surface always earns the one confirm pass.

## Examples

The confirm-pass prompt shape (targeted, not a from-scratch re-review):

```
Your prior pass returned NO-SHIP with these N findings. All N are folded
(commits <a> + <b>): [one line each]. Confirm on the CURRENT branch:
(a) are all N closed? (b) did the ~<K> lines of fold code introduce any NEW
issue, especially on the trust surface? Return APPROVE or concrete new
findings (file:line + failure scenario + severity).
```

Related:
- The `adversarial-review-grades-against-ideal` operating note (triage a cross-model NO-SHIP into defect-vs-deferred; don't stampede) is the *what-to-fold* half; this doc is the *verify-the-fold* half.
- [An existing repo guard flips a deferred residual into a required fold](existing-repo-guard-flips-a-deferred-residual-to-a-fold.md) — a cross-model finding changing a fold decision, a sibling to the "the review is not done when you think it is" theme.
