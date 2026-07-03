---
title: "Sanitize attacker-controlled fields in a render that IS a human-approval control"
date: 2026-06-19
last_updated: 2026-07-02
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A rendered output is itself a security control — a human approves what it shows before an action runs"
  - "That render includes fields an attacker can influence (config, file contents, model output, user input)"
  - "The output is shown in a terminal or any sink that interprets control/escape sequences"
  - "You are ADDING a new surface (a warning, summary, or log line) that renders the same untrusted data — it inherits every obligation below"
  - "Malformed/untrusted input could make the approval surface CRASH, not just spoof — a gate that tracebacks is also a failed control"
tags: [security, terminal-escapes, sanitization, trust-boundary, human-in-the-loop, preview, ansi, malformed-input, degrade-gracefully, fail-safe]
---

# Sanitize attacker-controlled fields in a render that IS a human-approval control

## Context

Cadre's agent-run handoff makes a `--preview` of the parsed fleet **the operative control**: a human reads the preview and okays it before the fleet runs. The design deliberately renders from the *parsed* `FleetConfig` (not the agent's paraphrase) so the human sees what will actually run. A cross-model adversarial review found that wasn't enough: a fleet YAML is attacker-controllable (library tampering), and `render_fleet_preview` printed its strings (`synthesis.prompt`, `role`, `focus`, provider/model, name) **raw**. Embedded ANSI/cursor escapes could clear lines, move the cursor, or overwrite the already-printed `⚠ PRIVILEGED TOOLS ENABLED` warning — spoofing the very output the human approves, even when the agent followed the procedure correctly.

## Guidance

If a rendered surface *is* a trust/approval control, treat every field that flows into it from an untrusted source as hostile to the **display**, not just to the logic. Strip terminal-control bytes before rendering:

```python
def _sanitize(text: str, *, multiline: bool = False) -> str:
    # Drop C0 controls (0x00-0x1F), DEL (0x7F), and C1 (0x80-0x9F). Removing the
    # ESC/CR/BS bytes defangs any sequence (a residual "[2J" renders as inert text).
    # Keep \n only where multi-line content is legitimate; printable Unicode
    # (>= 0xA0) passes through, so legit prompts render byte-identically.
    return "".join(
        ch for ch in text
        if (ch == "\n" and multiline) or (0x20 <= ord(ch) <= 0x7E) or ord(ch) >= 0xA0
    )
```

Apply it to *each* attacker-controlled field; keep your own labels and warnings as separate, un-tainted strings so they can't be hidden. Drop newlines in single-line fields so a field can't inject a fake line; preserve them only where multi-line text is expected. You do **not** need a full ANSI parser — dropping the ESC byte alone neutralizes the sequence.

### Three completeness traps (each bit Cadre — caught by successive review passes)

1. **Sibling surfaces.** Cover *every* surface that renders the same attacker-controlled data, not just the one where the bug was first found. Cadre hardened the *preview* (`render_fleet_preview`) but left the symmetric post-run output (`render_result`) — which renders the same config-derived `role`/`provider`/`model` and is what the agent weaves back as its honesty signal — unsanitized, so a tampered fleet could forge a `[ok  ] ghost (x/y)` provenance row. When you add sanitization, grep for *every* reader of those fields and treat them as one unit. **This recurs — treat it as a property of the surface, not a per-field fix.** A later change (PR #21) added a *new* warning surface (the `preview_lint` palette/focus warnings, printed on the same `--preview`/`validate` output) and re-introduced raw interpolation — and this time the unsanitized vector included an env-derived *path* (`CADRE_PALETTE`), not just fleet fields. Three separate review passes (persona, cross-model, PR bots) each found a *different* unguarded spot on the same surface. Piecemeal per-field patching guarantees the next reviewer finds another hole; funnel every external string the surface prints through one sanitizing chokepoint. **This recurred, larger, in #5 trust-safety Pass 1 (PR #46):** four independent passes each found *another* unsanitized sink well beyond the render (run-capture files, the manifest's identity/toolset/grade-entry fields, the all-failed failure note, the judge's raw text), and the fix promoted `render._sanitize` to the public `fleet_engine.text_safety.sanitize` module (GH #23) that every sink funnels through. The generalized "enumerate every sink and funnel them through one *auditable* chokepoint" method — where "does every sink import the chokepoint?" replaces the unprovable "did we patch every field?" — is documented as its own discipline: `docs/solutions/best-practices/enumerate-every-sink-through-one-sanitizing-chokepoint.md`.
2. **Beyond C0/C1.** Stripping ASCII control bytes (C0 `0x00–0x1F`, DEL, C1 `0x80–0x9F`) is necessary but not sufficient: Unicode line/paragraph separators (U+2028, U+2029) and bidi format controls (U+202A–U+202E, U+2066–U+2069) — plus the directional marks ALM/LRM/RLM (U+061C, U+200E, U+200F), which #46 added — sit at code points `>= 0xA0` and sail through a naive "keep `>= 0xA0`" allowance, re-enabling the fake-line / display-spoof. Exclude them explicitly.
3. **Spoofing isn't the only failure — the surface must also never *crash*.** A trust/approval gate that tracebacks on malformed input is as broken as one that's spoofed: the human never gets their go/no-go. The same fields and env inputs that carry escapes can also be *malformed* — non-UTF-8 bytes, an embedded NUL (`Path(...)` raises `ValueError`), or a YAML value of the wrong type (a list where a string is expected → `TypeError` on a set-membership test). Every external read that feeds the surface must **degrade, never raise**: catch the structural errors (`OSError`, `UnicodeDecodeError`, `ValueError`, `TypeError`) and fall back to a safe placeholder / "validation skipped", exactly as the escape-sanitizer falls back to inert text. In PR #21 this one surface drew a spoof finding *and* three independent crash findings (malformed `convergence`, a non-UTF-8 palette, a NUL in a path) — one each from the persona review, the cross-model pass, and the PR bots.

### Draw the boundary at config-vs-content

Sanitize the *identity/structural* fields the control vouches for (here: fleet name, role, provider, model — the provenance signal). Leave *model-generated content* (the report body, a lane's `text`/`error`) to the separate, harder injection-handling layer: stripping its control bytes is display-hardening, but its *semantic* injection (instructions a downstream agent might obey) is a distinct, deferred problem (Cadre tracks it as the injection->terminal chain). Conflating the two either under-protects the trust signal or over-promises on untrusted content.

## Why This Matters

"Render from the parsed config, not the paraphrase" makes the *content* faithful, but the *rendering* is still exploitable if the faithful content carries control sequences. A spoofed preview defeats the entire human-in-the-loop control silently — the human approves a screen that lies about `allow_privileged_tools` or the synthesizer. Once a render is load-bearing for a human decision, its trustworthiness depends on output sanitization as much as on content fidelity.

A related trap surfaced in the same review: a *run-time confirmation flag* (e.g. `--allow-privileged`) is **not** a control against an adversary who controls the invocation — an injection that can craft `--fleet evil.yaml --task …` can append the flag too, and the flag must stay documented for legitimate use. A real control must be a non-forgeable artifact bound to exactly what was shown (tracked separately — see Related).

## When to Apply

- Any preview/approval/confirmation surface that renders untrusted data for a human go/no-go.
- Logs or audit output that a human is expected to *trust* and that include third-party strings.
- Any durable record that carries the same untrusted fields — a run-capture file, a manifest, a machine-read grade — inherits the obligation even when no human gates on it directly. #5 Pass 1 extended the `text_safety` chokepoint to exactly these sinks, past the original approval-render scope.
- Not needed for purely internal data that never gates a human decision.

## Examples

Before — raw interpolation; a tampered `focus`/`synthesis.prompt` can overwrite the warning:

```python
out.append(f"  [{s.role}]  {s.provider}/{s.model}  toolset={toolset_str}")
out.append(f"    focus: {s.focus}")
```

After — every fleet-controlled field sanitized; labels/warnings stay separate:

```python
out.append(f"  [{_sanitize(s.role)}]  {_sanitize(s.provider)}/{_sanitize(s.model)}  toolset={toolset_str}")
out.append(f"    focus: {_sanitize(s.focus)}")
```

Test the control, not just the formatter: assert that an ESC/CR in a field cannot strip those bytes from the output **or** hide the privileged-tools line, and that a clean fleet renders byte-identically (no false-positive stripping of legitimate punctuation/Unicode).

## Related

- `docs/solutions/security-issues/empty-toolset-collapsed-to-all-tools.md` — another case where an attacker-controlled boundary in the same surface bypassed a stated control.
- `docs/solutions/design-patterns/fail-closed-allowlist-for-capability-gates.md` — the config-level gate the preview visually represents.
- `docs/solutions/design-patterns/enumerate-consumers-when-a-new-value-aliases-a-load-bearing-state.md` — the same "grep for every reader/consumer and treat them as one unit" discipline, applied to a result field instead of a render surface.
- `docs/solutions/design-patterns/in-band-marker-nonce-defends-echo-not-injection.md` — a sibling output-integrity defense from the same #5 Pass 1: a per-run nonce authenticates a structured judge marker so echoed/quoted text can't forge it (defends accidental echo, not semantic injection).
- GitHub issue #5 — the deferred security pass (a non-forgeable, preview-bound approval artifact; live toolset verification).
