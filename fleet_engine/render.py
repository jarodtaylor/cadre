"""Render a FleetResult for human-facing entry surfaces (CLI, Hermes skill).

Kept out of the engine — the engine returns structured data, each surface
formats it — and shared here so the skill renders without depending on the CLI
command layer.
"""

from __future__ import annotations

from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult

# Predicate: synthesizer looks API-billed (Anthropic/Claude/Opus) inside Hermes.
# False-negatives (missed expensive runs) are worse than false-positives (extra
# warnings), so we err toward flagging. The raw provider/model string is always
# shown so a missed heuristic can never hide an expensive run.
_BILLED_KEYWORDS = ("anthropic", "claude", "opus")


def _looks_api_billed(provider: str, model: str) -> bool:
    combined = f"{provider}/{model}".lower()
    return any(kw in combined for kw in _BILLED_KEYWORDS)


def render_fleet_preview(config: FleetConfig) -> str:
    """Render a human-readable preview of a FleetConfig.

    Derived MECHANICALLY from the validated ``FleetConfig`` object — never from
    prose or agent paraphrase. The human approves this output, not the agent's
    summary of it, which is why it must be complete and exact.

    Returns a multi-line string suitable for terminal display.
    """
    out = [f"=== fleet preview: {config.name} ==="]

    # --- synthesizer ---
    synth_str = f"{config.synthesis.provider}/{config.synthesis.model}"
    out.append(f"\nSynthesizer: {synth_str}")
    if _looks_api_billed(config.synthesis.provider, config.synthesis.model):
        out.append("  ⚠ bills at API rates inside Hermes")

    # --- privileged tools flag ---
    if config.allow_privileged_tools:
        out.append("\n⚠ PRIVILEGED TOOLS ENABLED (allow_privileged_tools: true)")
    else:
        out.append("\nallow_privileged_tools: false")

    # --- synthesis prompt ---
    prompt_text = config.synthesis.prompt.strip() if config.synthesis.prompt else "(none)"
    out.append(f"\nSynthesis prompt:\n  {prompt_text.replace(chr(10), chr(10) + '  ')}")

    # --- specialists ---
    out.append(f"\nSpecialists ({len(config.specialists)}):")
    for s in config.specialists:
        toolset_str = ", ".join(s.toolset) if s.toolset else "(none)"
        out.append(f"  [{s.role}]  {s.provider}/{s.model}  toolset={toolset_str}")
        if s.focus:
            out.append(f"    focus: {s.focus}")

    out.append("\n=== end preview ===")
    return "\n".join(out)


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
