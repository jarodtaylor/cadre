---
title: "Make a narrowing default non-destructive, don't just disclose what it destroys"
date: 2026-07-06
category: design-patterns
module: "cadre/verify_palette.py (_cap_candidates's always_keep, _existing_palette_pairs); generalizes to any operation that rewrites durable state wholesale under a new limiting default"
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A change introduces a narrowing/limiting default (a cap, a page size, a sample, a cutoff) on an operation that REWRITES durable, hand-curated, or previously-established state from scratch rather than merging with it"
  - "Some of what already exists in that state was established under the OLD, unbounded behavior and would fall outside the new default's window on a routine, non-adversarial re-run"
  - "The action can run unattended (a script, an install step, CI) or under an autonomous/agent operator, not only an interactively-supervised human"
  - "The first-instinct fix under consideration is 'warn about it,' not 'prevent it'"
tags: [default-behavior, non-destructive, disclosure, spend-control, durable-state, agent-operator, cross-model-review]
---

# Make a narrowing default non-destructive, don't just disclose what it destroys

## Context

Cadre's #61 (palette auto-discovery) added a default per-provider verify cap to `cadre verify-palette`: verifying a discovered candidate means a real, paid call against a live provider, so by default only the first 2 candidates per provider are verified (`--all` verifies everything). `verify-palette`'s `main()` writes `palette.yaml` **from only the current pass's verification records** — a wholesale rewrite, not a merge, because a palette entry means "verified as of the last run" and merging in stale, unverified entries would make that claim dishonest.

Those two decisions collided. A routine re-verify — adding one new candidate, or simply re-running `verify-palette` to double-check something — would verify only the capped first two per provider, then rewrite `palette.yaml` from just those records, silently dropping any previously-verified pair that fell outside the cap. Any fleet composed against a dropped pair would then start failing preflight (the #62 off-palette refusal): an innocuous, flagless `cadre verify-palette` could silently strand an already-working fleet.

The first fix was a pre-spend stderr warning: before the first paid verify call, print the previously-verified pairs about to drop and name `--all` or re-adding them to the candidates file as how to keep them. That fix passed four same-model review lenses plus the invariant reviewer with no objection to the *approach* — only refinement requests, already satisfied by firing the warning before spend. It was the cross-model adversarial pass (Codex) that rejected the approach itself: **"The stderr warning does not prevent this in scripts or agent-driven flows."** A disclosure defends an interactive human reading a terminal in real time; it does nothing for `scripts/install.sh`, which runs `verify-palette` unattended as part of setup, taking the capped default with no `--all` — and it does nothing for an agent operator either, which matters specifically here, since this project's own strategy explicitly names the AI agent, not the interactive human, as the primary operator.

## Guidance

The durable fix changed **what gets rewritten**, not just what gets said about it:

```python
# cadre/verify_palette.py — before: the cap alone decided what survives a
# rewrite, with no way to exempt an already-established pair.
def _cap_candidates(candidates, per_provider=_DEFAULT_PER_PROVIDER_CAP):
    seen: dict[str, int] = {}
    capped: list[tuple[str, str]] = []
    for pair in candidates:
        provider = pair[0]
        count = seen.get(provider, 0)
        if count < per_provider:
            capped.append(pair)
            seen[provider] = count + 1
    return capped

# after: an always_keep set exempts existing-palette pairs from the cap —
# they don't count toward it and can't be excluded by it.
def _cap_candidates(candidates, per_provider=_DEFAULT_PER_PROVIDER_CAP,
                     always_keep=frozenset()):
    seen: dict[str, int] = {}
    capped: list[tuple[str, str]] = []
    for pair in candidates:
        if pair in always_keep:
            capped.append(pair)
            continue
        provider = pair[0]
        count = seen.get(provider, 0)
        if count < per_provider:
            capped.append(pair)
            seen[provider] = count + 1
    return capped
```

```python
# main(): the existing palette's own pairs feed the exemption, so a capped
# re-verify RE-VERIFIES every previously-verified pair instead of silently
# pruning it.
existing = _existing_palette_pairs(palette_path)
to_verify = (
    candidates
    if all_candidates
    else _cap_candidates(candidates, always_keep=frozenset(existing))
)
```

A pair now leaves the palette exactly two ways, and both are honest: it fails re-verification (the provider/model genuinely stopped working), or it was removed from the candidates file entirely (an explicit, visible edit — by the operator, or by a fresh `cadre discover`). Never "it existed, but didn't fit in this pass's arbitrary window." The stderr warning didn't disappear — it narrowed to the one remaining case that's still genuinely destructive and *can't* be fixed by re-verifying: a pair no longer in the candidates file at all. That residual is disclosed because there's nothing left to make non-destructive about it; the cap-related loss is gone.

Generalize this as a decision rule: when a new **narrowing** default (a cap, a page limit, a sample, a time window) governs a **rewrite** of durable state, ask "does anything valid under the OLD, unbounded behavior fall outside this new default's window?" If yes, don't reach for a warning first. Check whether the entries that would be silently lost can instead be **exempted from the new limit** (re-validated, re-included, re-processed) so the rewrite's output is always a superset of "everything from before that's still valid" plus "whatever the new limit adds." Reserve the warning for the residual that genuinely can't be saved this way — here, a pair that left the *input* file, which is a fact about the source, not about the limit.

## Why This Matters

A disclosure is a guardrail with exactly one enforcement mechanism: a human reads it and chooses to act differently. That mechanism has a real, common failure population — every unattended script, every CI job, every autonomous agent operator, and every human who has seen fifty other stderr warnings today and pattern-matches "probably fine." This project's own operator model names the AI agent as primary, not the interactive human at a terminal — so a fix whose only enforcement is "the reader notices and intervenes" targets the wrong consumer for this codebase specifically, and has a real failure mode for nearly any codebase generally. `scripts/install.sh` is the concrete case: a shell script has no eyes to read stderr with, and even a human running it interactively is watching install output scroll by, not auditing a palette diff line by line.

Making the default non-destructive removes the dependency on anyone reading anything. The rewrite's own output is safe **by construction** — a previously-valid entry can only be lost for a reason a warning couldn't have prevented anyway (it actually failed re-verification, or it was deliberately removed from the source). That is a strictly stronger guarantee than "we told you it would happen," and it costs less than it looks like: re-verifying a handful of already-established pairs is cheap against the spend the cap exists to bound in the first place, because the cap's real job was always to bound the *discovery flood*, not to bound how much of an *already-trusted* palette gets reconfirmed.

The meta-lesson is worth banking on its own. The author's first-instinct fix — warn about it — was reviewed clean by four same-model lenses and rejected only by the cross-model adversarial pass. Same-model review tends to accept the same framing of the problem the author used: "disclose it" felt like *the* fix because the author was already thinking in terms of "make the operator aware." A genuinely different reviewing perspective was needed to question the framing itself — aware, yes, but through what enforcement mechanism, and for which consumer? That is the same shape of catch this repo has banked from cross-model review before: its value is as much about surfacing a different *frame* as it is about catching a different *bug*.

## When to Apply

- A new default introduces a cap, limit, sample, page size, or cutoff on an operation that **rewrites** (not merges) a durable, hand-curated, or previously-established record.
- Some of what's already in that record was established under the OLD, unbounded behavior and would fall outside the new default's window on a routine re-run — not only on a deliberately adversarial input.
- The action can run unattended (a script, CI, an install step) or under an agent/autonomous operator, not only an interactively-supervised human — which is close to "always," so default to running this check rather than assuming a human is watching closely enough to intervene.
- A first-instinct fix under discussion is "warn about it" — treat that instinct itself as the signal to ask the harder question (can the loss be *prevented*, not just disclosed) before shipping the warning as the whole fix.

Skip the extra work only when the destructive path truly cannot be avoided — the entries that would be lost are, themselves, no longer valid, not merely outside an arbitrary window. That is exactly the residual this pattern still discloses rather than tries to preserve.

## Examples

**Before:** `verify-palette`'s default cap could rewrite `palette.yaml` from only the capped pass's records, silently dropping any previously-verified pair beyond the cap; the first fix was a pre-spend stderr warning naming the pairs about to drop.

**After:** `_cap_candidates` gained `always_keep`, fed by the existing palette's own pairs — a capped re-verify now re-verifies every previously-verified pair unconditionally, so the only pairs that can still drop are ones that failed re-verification or were removed from the candidates file entirely. The warning persists, narrowed to exactly that un-fixable remainder.

**Generalized shape:** a search-index rebuild adds a default `--limit 1000` to bound reindex cost. A collection with 1,200 documents would silently drop 200 previously-indexed documents from the rebuilt index on a routine, unattended reindex. "Log a warning that 200 documents will be dropped" protects a human watching the job's logs in real time; it does nothing for the nightly cron that runs it unattended. The non-destructive fix: documents already present in the index are exempt from the new limit — revalidated against the source rather than naively kept — so the limit bounds how many *new* documents get added in one pass, not how many *already-indexed* documents survive it.

## Related

- `disclose-material-facts-on-the-approval-surface.md` — the sibling pattern this doc is the limit case of. That doc establishes disclosure as the fix when a surface silently transforms input a human approves; this doc is the boundary condition where disclosure alone is not enough, because "a human reads it and intervenes" has a real, common failure population — scripts, unattended installs, agent operators. Apply that doc's disclosure discipline for the residual that genuinely can't be made non-destructive; reach for this doc's stronger fix whenever the loss can instead be prevented.
- `../best-practices/verify-the-consumer-relies-on-the-defense-property.md` — the same "who is the real consumer, and what do they actually rely on" method, applied one layer up: there, the question is whether a *display* defense reaches every *reader*; here, the question is whether a *disclosure* defense reaches every *actor who could intervene* on it. Both catches came from asking which consumer actually benefits from a control, rather than accepting that the control existed.
- `distinguish-absent-from-invalid-in-a-degrade-open-gate.md` — same #61/#62-wave sibling with the same meta-shape ("don't conflate two states under one uniform default") and the same provenance: cross-model review catching what a same-model panel cleared.
- GitHub issue #61 (palette auto-discovery) — the feature this pattern was extracted from; `cadre/verify_palette.py`'s `_cap_candidates`/`always_keep`/`_existing_palette_pairs` is the concrete implementation.
