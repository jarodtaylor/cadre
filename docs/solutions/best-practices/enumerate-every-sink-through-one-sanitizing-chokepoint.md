---
title: "Enumerate every sink and funnel them through one auditable chokepoint"
date: 2026-07-03
category: best-practices
module: "fleet_engine trust-safety (text_safety chokepoint); generalizes to any cross-cutting exit-point transform"
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - "Hardening output against untrusted or attacker-influenced text (web results, doc/file input, model output, cross-lane threaded content)"
  - "A cross-cutting transform (sanitize, escape, redact, authorize) must hold at every exit point, not just where the first bug surfaced"
  - "Independent reviewers keep finding 'one more' unhandled sink during the same pass"
  - "Adding a new surface (a log line, manifest field, capture file, record) that could carry the same data as an already-handled surface"
tags: [sanitization, trust-boundary, chokepoint, code-review, review-completeness, security, defense-in-depth, cross-cutting]
---

# Enumerate every sink and funnel them through one auditable chokepoint

## Context

Cadre fans a task out across multiple models and folds untrusted content back in at several points — web results, `--doc` file contents, sibling-lane output threaded into later rounds, and the model output itself. Any of those strings can end up in front of a human (the terminal) or in a durable record (a run-capture file, the manifest). A system like this doesn't have one output sink, it has several, and they tend to accumulate over time as new features add new surfaces (a new render line, a new capture file, a new manifest field).

During Cadre's #5 trust-safety Pass 1 build (PR #46, merged `41f43ae`; the sanitizing chokepoint itself is issue #23), the team hardened output so attacker-influenced text can't smuggle ANSI/cursor escapes or Unicode bidi/line-separator controls into a surface a human reads or a record the system trusts — for example, overwriting a printed `⚠ PRIVILEGED TOOLS ENABLED` warning, or forging a fake `[ok] ghost` line.

That hardening went through four independent review passes: `ce-code-review`, a cross-model Codex adversarial pass, Copilot, and CodeRabbit (both inline and its "outside the diff" comments). Each pass found **another** sink still printing attacker-influenced text unsanitized — in sequence: the terminal render path, then the run-capture files, then the manifest's identity/toolset/grade-entry fields, then the all-failed failure note, then the judge's raw text. Roughly four rounds of "found one more place." Fixing each as it surfaced never converged — every review closed one gap and opened the question of whether another one existed. The build only stabilized once the approach stopped being "patch this field" and became "one function every sink must call."

## Guidance

The method, in order:

1. **Enumerate every sink before writing any handling code.** Write down, concretely, every surface that emits the data the rule must govern: terminal stdout/stderr, run-capture files on disk, the manifest/metadata record, logs, preview output, and any downstream consumer that reads these (a skill runner, an agent weaving a result back). Treat this as a first-class step with a visible list, not something you'll "get to" per-field as you notice it.

2. **Funnel all of them through one public chokepoint.** Don't give each sink its own inline logic. A private, site-local helper (Cadre's original `render._sanitize`) invites exactly the failure mode above: it's easy to reason about "this one render call is safe" and just as easy to forget that capture, the manifest, and the failure-note path all format the same untrusted strings independently.

3. **Make completeness auditable, not just asserted.** The payoff of step 2 is that "did we handle it everywhere?" — an open-ended, unprovable question when logic is scattered — becomes "does every sink import and call the chokepoint?", a closed, greppable one. A reviewer can list the sink files and check imports in minutes instead of re-deriving the full data flow from scratch.

Concretely, Cadre promoted the private `render._sanitize` helper to a public module, `fleet_engine.text_safety.sanitize()` (GH #23). Its own module docstring states the lesson directly:

> "Funnel every attacker-influenced string a control or display surface prints through `sanitize` — piecemeal per-field patching guarantees the next reviewer finds another hole."

After the promotion the sinks route through one function — `render`, `capture`, and `cli` import it directly (`from fleet_engine.text_safety import sanitize`), and `render._sanitize` remains as a compat alias for the remaining call sites. Verifying completeness is now a one-line grep across the known sink files, not a fresh audit of every string-formatting call site in the codebase.

## Why This Matters

Piecemeal patching guarantees the next reviewer finds another hole — that's not a hypothetical, it's what happened here four times in sequence, across four *independent* reviewers (same-model, cross-model, and two different bots). Each miss wasn't a redundant finding; it was a genuinely different sink that had shipped unhandled until someone looked at it specifically. That means each gap was a live, exploitable window in the interval between when the first sink got fixed and when the last one was found — not a theoretical residual risk.

The chokepoint's value is that it bounds the search space for review. "Did we handle every attacker-influenced string in the system" is an open question over an unenumerable set of call sites — you can never be confident you've checked the last one. "Does every sink import and call the chokepoint" is a closed question over an enumerable set of files — checkable, greppable, and stable as new sinks get added (the bar for a new surface becomes "did you import the chokepoint," not "did you re-derive the logic").

This generalizes past output sanitization: it's the same "normalize/validate at the boundary" discipline Cadre already applies to *inputs* (e.g. coercing `FleetStatus` through its `str`-Enum at the `__post_init__` boundary rather than trusting every caller to pass a valid value) — here applied to *outputs*. Instead of normalizing everything as it enters, you funnel everything through one gate as it leaves.

**Scope, stated honestly (don't overclaim).** In the case that produced this learning, the chokepoint defends against escape-spoofing and accidental display-forgery — stripping the control bytes and Unicode bidi/separator characters that let one printed line overwrite, hide, or fake another. It does **not** address semantic prompt injection or report-grammar mimicry: a model can still print plain, printable ASCII that *reads* like a system line (e.g. a literal `[ok ] ghost` string or a fake `--- role ---` header) and `sanitize()` will pass it through untouched, because nothing about it is a control character. That gap is tracked separately (Cadre's fast-follow #45) and is a distinct problem from the one this chokepoint solves. The enumerate-then-funnel *method* is orthogonal to which transform the chokepoint performs — it makes coverage auditable; it does not make the transform stronger than it is.

## When to Apply

Reach for an enumerate-then-funnel chokepoint whenever a single cross-cutting rule must hold at *every* exit or entry point of a system that has (or will grow) more than one:

- Output sanitization/escaping across multiple display or write surfaces (this case).
- Redacting secrets from logs, when more than one code path can log.
- Authorization/permission checks that must apply to every route or handler.
- Encoding/escaping at every serialization boundary (SQL parameterization, HTML escaping, shell quoting).
- Any place a review-comment pattern turns into "oh — one more spot" more than once. That recurrence is the signal: stop patching the found instance and consolidate instead of waiting for the next reviewer to find the next one.

Skip the indirection when there's genuinely one sink and good architectural reason it will stay that way — a single well-scoped helper called from one place doesn't need to become a shared module. The pattern earns its cost specifically when sinks are already plural, or predictably will be as the system grows new surfaces.

## Examples

**Before:** `render._sanitize`, a private, render-module-local helper. It covered the terminal render path because that's where the first review found the problem. Independently and in sequence, reviewers then found: the run-capture files formatted the same fields without calling it; the manifest's identity/toolset/grade-entry fields wrote the same untrusted strings straight to JSON; the all-failed failure note interpolated raw text; the judge's raw text rendered unsanitized. Each state looked locally complete — the sink someone had just checked was clean — until the next reviewer looked at a sink nobody had audited yet.

**After:** `fleet_engine.text_safety.sanitize()` (GH #23, landed in PR #46), a public function with the completeness rule written into its own docstring. Every known sink routes through it. Checking completeness is now: list the sink files, confirm each one funnels through `fleet_engine.text_safety`, done.

## Related

- `docs/solutions/design-patterns/sanitize-trust-surface-renders-against-terminal-escapes.md` — the concrete worked example of this discipline and where it was first discovered (its "trap #1 — sibling surfaces"): sanitizing the specific fields that flow into a human-approval render, which characters to strip (C0/C1 plus Unicode bidi/separator controls), and why a render that gates a human decision is a trust surface. That doc is the *what to strip*; this one is the generalized *how to be sure you found every sink*.
- `docs/solutions/design-patterns/in-band-marker-nonce-defends-echo-not-injection.md` — a sibling output-integrity defense from the same #5 Pass 1: a per-run nonce authenticates a structured judge marker so echoed/quoted text can't forge it (defends accidental echo, not semantic injection).
- `docs/solutions/design-patterns/disclose-material-facts-on-the-approval-surface.md` — a related discipline on what an approval surface owes the human reading it.
