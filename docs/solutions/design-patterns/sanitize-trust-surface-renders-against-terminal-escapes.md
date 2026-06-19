---
title: "Sanitize attacker-controlled fields in a render that IS a human-approval control"
date: 2026-06-19
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A rendered output is itself a security control — a human approves what it shows before an action runs"
  - "That render includes fields an attacker can influence (config, file contents, model output, user input)"
  - "The output is shown in a terminal or any sink that interprets control/escape sequences"
tags: [security, terminal-escapes, sanitization, trust-boundary, human-in-the-loop, preview, ansi]
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

## Why This Matters

"Render from the parsed config, not the paraphrase" makes the *content* faithful, but the *rendering* is still exploitable if the faithful content carries control sequences. A spoofed preview defeats the entire human-in-the-loop control silently — the human approves a screen that lies about `allow_privileged_tools` or the synthesizer. Once a render is load-bearing for a human decision, its trustworthiness depends on output sanitization as much as on content fidelity.

A related trap surfaced in the same review: a *run-time confirmation flag* (e.g. `--allow-privileged`) is **not** a control against an adversary who controls the invocation — an injection that can craft `--fleet evil.yaml --task …` can append the flag too, and the flag must stay documented for legitimate use. A real control must be a non-forgeable artifact bound to exactly what was shown (tracked separately — see Related).

## When to Apply

- Any preview/approval/confirmation surface that renders untrusted data for a human go/no-go.
- Logs or audit output that a human is expected to *trust* and that include third-party strings.
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
- GitHub issue #5 — the deferred security pass (a non-forgeable, preview-bound approval artifact; live toolset verification).
