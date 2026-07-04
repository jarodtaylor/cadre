---
title: An existing repo guard flips a deferred "residual" into a required fold
date: 2026-07-03
category: best-practices
module: code-review
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - "Triaging a code-review finding you are about to mark out-of-scope or residual"
  - "A cross-model review flags a finding that same-model passes deferred"
  - "Hardening a trust surface whose integrity rests on filesystem permissions, not crypto"
tags: [code-review, cross-model-review, review-triage, security-review, precedent, fail-closed]
---

# An existing repo guard flips a deferred "residual" into a required fold

## Context

While hardening the preview-bound approval (GitHub #5 Part 2), a Codex cross-model
adversarial review flagged that the approval token — which carries no MAC (the digest
is a hash of inputs the agent already holds) — could be replaced by a co-resident user
if its parent directory (`~/.cadre`, or a `CADRE_APPROVAL_PATH` override) were
group/other-writable.

Four *same-model* review lenses (correctness/adversarial, security, docs/tests, and the
repo's invariant reviewer), the advisor, and the orchestrator had all independently
examined this exact surface and **deferred** it as an "AE6 / single-operator residual" —
out of scope for the stated threat model. The frame was wrong, and every same-model pass
shared it. What broke the frame was not a stronger argument but a **checkable fact**: the
repo already guards this class of surface. `fleet_engine/personas.py` (`resolve`) stats
the persona-pool directory and fails closed when it is not owner-owned or is
group/other-writable — because the pool's files are read verbatim as model instructions,
so its integrity rests on filesystem permissions. The approval token has the *same*
property, only stronger (no MAC at all). Not applying the same check was an
inconsistency, not a scoped-out choice.

## Guidance

**Before you finalize a decision to defer a review finding as an out-of-scope residual,
search the codebase for an existing guard on the same class of surface.** If the repo
already defends that class, the finding is an inconsistency to fold, not a residual to
defer — and the precedent is objective, checkable evidence that overrides a
unanimous-but-subjective "residual" call.

When triaging a finding you are inclined to defer:

1. Name the *class* of the surface (here: "a trust artifact whose integrity rests on
   filesystem permissions, no crypto").
2. `grep` for an analogous guard elsewhere in the repo (here: `st_uid != getuid()`,
   `st_mode & 0o022`, ownership/permission checks).
3. If one exists, the default flips from *defer* to *fold* — mirror the established guard
   rather than re-arguing whether the risk is "in scope."

The fix mirrored the precedent almost verbatim:

```python
# fleet_engine/approval.py — mirrors personas.resolve's pool-dir ownership/mode check
def _parent_is_safe(parent: str) -> bool:
    try:
        st = os.stat(parent)
    except OSError:
        return False
    getuid = getattr(os, "getuid", None)
    if getuid is not None and st.st_uid != getuid():
        return False
    if st.st_mode & 0o022:   # group- or other-writable
        return False
    return True
```

## Why This Matters

A unanimous "defer as residual" from several reviewers reads like a conclusion but can be
a **shared blind spot** — especially when the reviewers share a model family, since they
reinforce the same framing rather than challenging it. The disconfirming signal is often
not a cleverer argument (which same-model reviewers are unlikely to produce, having all
anchored the same way) but an existing precedent in the same codebase: "we already defend
this exact class over there." A precedent is objective and cheap to check, and
consistency with an established guard is itself a correctness property — divergent
handling of the same trust-surface class is a latent bug regardless of whether the
specific finding is "in scope."

This is the concrete tie-breaker behind the more general observation that a cross-model
pass catches what N same-model passes miss: what the cross-model reviewer actually *did*
was cite the repo's own pattern. You do not always need the cross-model reviewer to get
there — a precedent grep during triage reaches the same conclusion.

## When to Apply

- Triaging any review finding you are about to mark "out of scope," "documented
  residual," or "won't fix" — run the precedent grep first.
- Especially when multiple same-model reviewers (or the advisor, which is also same-model)
  agree to defer — unanimity there is weak evidence, not strong.
- Especially for security/robustness findings on a surface whose integrity is enforced by
  convention (permissions, ownership, ordering) rather than a hard mechanism.

## Examples

- **Deferred → folded (this case):** the approval token's parent-directory permission
  check. Same-model reviewers deferred it as "needs $HOME-write / AE6 residual"; the
  persona-pool precedent showed the repo already fails closed on this class, so it became
  a required fold with a fail-closed test on both write (`raises`) and consume
  (`returns None`). Commit `cd619c3`.
- **The distinction that matters:** a *symlink-plant* on the same parent genuinely does
  need `$HOME`-write and stayed a residual; the *permission* angle (a group/writable dir,
  no `$HOME`-write needed) did not. Naming the class precisely is what separates the real
  residual from the inconsistency — the earlier defer had conflated the two.

## Related

- `docs/solutions/design-patterns/fail-closed-allowlist-for-capability-gates.md` — the
  fail-closed posture this guard extends.
- `docs/solutions/best-practices/lens-decomposition-vs-model-diversity-in-review-fleets.md`
  — why review coverage comes from lens decomposition plus model diversity.
- `docs/solutions/security-issues/empty-toolset-collapsed-to-all-tools.md` — a prior
  security finding an adversarial pass surfaced after same-model review looked clean.
- GitHub #5 (trust-safety) — the feature this arose in.
