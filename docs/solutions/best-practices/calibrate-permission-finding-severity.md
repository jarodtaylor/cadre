---
title: "Fix the permission window, but calibrate its severity against the directory chain"
date: 2026-06-18
category: best-practices
module: fleet_engine
problem_type: best_practice
component: service_object
severity: medium
applies_when:
  - "A reviewer (human or bot) flags a file-permission or TOCTOU window such as write-then-chmod"
  - "Writing owner-only files, especially under a caller- or env-supplied directory"
  - "Stating how severe a permission finding is in a PR reply, commit, or security note"
tags: [file-permissions, security-review, severity-calibration, defense-in-depth, toctou, run-capture]
---

# Fix the permission window, but calibrate its severity against the directory chain

## Context
Run capture writes prompts and model outputs to disk (`fleet_engine/capture.py`). Two reviewers (Copilot and CodeRabbit) independently flagged `_write` for a classic window: it called `path.write_text(...)` and *then* `path.chmod(0o600)`, so under a default umask the file existed briefly as `0o644` (group/other-readable) before being tightened. Both framed it as a "world-readable hole."

The fix is real — but the *severity* as stated was wrong, and that distinction is the reusable part.

## Guidance
Two moves, in order, whenever a file-permission or TOCTOU window is flagged.

**1. Fix it at creation, not after.** Open the file with the restrictive mode instead of write-then-chmod, so it is never momentarily loose:

```python
# before — momentary 0o644 under a default umask
path.write_text(content, encoding="utf-8")
path.chmod(0o600)

# after — owner-only at creation
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(content)
path.chmod(0o600)  # also tightens a pre-existing file: O_CREAT won't re-mode one that already exists
```

(`0o600` has no group/other bits, so a typical umask can't strip anything — the created file is exactly `0o600`.)

**2. Calibrate the severity against the enclosing directory chain before you accept it.** A file is only as reachable as the directories above it. Here the whole `~/.cadre/runs/<run>/` chain is created `0o700` (owner-only), so no other local user could traverse in to read the file during the window — the "world-readable" framing was an overclaim. The fix genuinely matters in the **explicit-dir** case: when `CADRE_RUN_DIR` points at a pre-existing, world-traversable directory, `mkdir(exist_ok=True)` will not tighten it, and the loose-file window is then actually reachable.

So: apply the fix (it is correct and cheap — defense-in-depth plus correct-at-creation), but describe it as *hardening the explicit-dir path*, not as *closing a world-readable breach*.

## Why This Matters
- **Under-reacting loses a real fix.** The window is genuine in the explicit-dir case; "the dir is `0o700` anyway" is not a reason to skip it.
- **Over-reacting has its own cost.** Overstating severity in a public PR reply or changelog is misleading, and it trains you (and readers) to treat every flagged window as a breach. An automated reviewer asserts a severity; it has not inspected your directory chain. You have to.
- The two moves are independent: do the fix *regardless*, and report the severity *honestly* after tracing the path.

## When to Apply
- Any reviewer (human or bot) flags a permission/TOCTOU window — write-then-chmod, a check-then-create race, a temp file with loose perms.
- Before you write the PR reply, commit message, or security note that states how bad it is.
- Especially when the path is partly caller- or env-controlled (an injected dir, a `*_DIR` env var), where your own directory-perm guarantees may not hold.

## Examples
Same review pass, opposite calibration — that contrast *is* the lesson:

- **This finding (calibrated down):** write-then-chmod under a `0o700` chain → reachable only in the explicit-dir case → fixed, severity medium, described as hardening (`fleet_engine/capture.py`, commit `b8d677c`).
- **A sibling finding (correctly high):** an empty toolset that collapsed to *all* tools was a genuine privilege bypass reachable via prompt injection — nothing above it gated the exposure, so "high" stood. See the Related link below.

The same question drives both — "what actually gates this?" — and it's the answer, not the instinct, that sets the severity.

## Related
- [Atomic directory reservation beats check-then-create](../design-patterns/atomic-directory-reservation-over-check-then-create.md) — the other half of `capture.py`'s filesystem hardening (the dir-creation race); same module, same review pass.
- [Side-effects at the edge: keep the engine core pure](../architecture-patterns/side-effects-at-the-edge-pure-engine-core.md) — why this file I/O lives in the caller-layer `capture.py` at all, not the engine.
- [Empty toolset collapsed to ALL tools (privilege bypass)](../security-issues/empty-toolset-collapsed-to-all-tools.md) — a finding from the same review lineage whose severity *was* high; the contrast in Examples.
