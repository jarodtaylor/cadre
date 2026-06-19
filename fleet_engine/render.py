"""Render a FleetResult for human-facing entry surfaces (CLI, Hermes skill).

Kept out of the engine — the engine returns structured data, each surface
formats it — and shared here so the skill renders without depending on the CLI
command layer.
"""

from __future__ import annotations

from fleet_engine.engine import FleetResult


def render_result(result: FleetResult) -> str:
    header = "synthesized result" if result.ok else "partial result (no synthesis)"
    out = [f"=== {result.fleet} — {header} ==="]
    if result.synth_ok is None:
        # All specialists failed — synthesis was never attempted. Surface a
        # prominent line so the caller never mistakes this for a valid result.
        n_failed = len(result.failures)
        n_total = len(result.specialists)
        out.append(f"No synthesis — {n_failed} of {n_total} specialists failed; synthesis was not attempted.")
    if result.synthesis:
        out.append(result.synthesis)
    elif result.successes:
        # No synthesis (synthesizer failed) but lanes succeeded — surface their raw
        # findings in labeled sections so the user still gets the work, not just
        # provenance rows.
        for r in result.successes:
            out.append(f"\n--- {r.role} ({r.provider}/{r.model}) ---\n{r.text}")
    out.append("\n--- provenance ---")
    for r in result.specialists:
        if r.ok:
            tag = "ok  "
        elif r.timed_out:
            tag = "TIMEOUT"
        else:
            tag = "FAIL"
        suffix = "" if r.ok else f": {r.error}"
        out.append(f"[{tag}] {r.role} ({r.provider}/{r.model}){suffix}")
    if result.notes:
        out.append("\nnotes:")
        out.extend(f"  - {n}" for n in result.notes)
    return "\n".join(out)
