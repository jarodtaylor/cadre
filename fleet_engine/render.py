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


# Unicode line/paragraph separators and bidi format controls are never legitimate
# in a fleet field; >=0xA0 would otherwise pass them through and re-enable the
# fake-line / display-spoof the C0/C1 strip closes.
_UNSAFE_UNICODE = frozenset(
    "  "                      # line / paragraph separators
    "‪‫‬‭‮"    # bidi embeddings / overrides
    "⁦⁧⁨⁩"          # bidi isolates
)


def _sanitize(text: str, *, multiline: bool = False) -> str:
    """Strip terminal-control characters from fleet-controlled text before display.

    A fleet YAML is attacker-controllable (library tampering — see the cadre-fleet
    SKILL.md Safety section), and its strings flow into this preview, which is the
    operative human-okay control. An embedded ANSI/cursor escape sequence could
    otherwise overwrite or hide a printed warning (e.g. the privileged-tools line),
    spoofing the very output the human approves. Drop C0 controls (0x00–0x1F),
    DEL (0x7F), and C1 (0x80–0x9F): removing the ESC/CR/BS bytes defangs any
    sequence (a residual ``[2J`` then renders as inert text). Also drops Unicode
    line/paragraph separators (U+2028, U+2029) and bidi format controls
    (U+202A–U+202E, U+2066–U+2069), which >=0xA0 would otherwise pass through and
    re-enable the fake-line / display-spoof the C0/C1 strip closes. Newlines survive
    only for the multi-line synthesis prompt; TAB is also preserved in multiline mode
    only; elsewhere both are dropped so a single-line field cannot inject a fake line.
    Printable Unicode (>= 0xA0) other than the excluded set passes through untouched,
    so a legitimate prompt renders byte-identically.
    """
    return "".join(
        ch
        for ch in text
        if (
            (ch == "\n" and multiline)
            or (ch == "\t" and multiline)
            or (0x20 <= ord(ch) <= 0x7E)
            or ord(ch) >= 0xA0
        )
        and ch not in _UNSAFE_UNICODE
    )


def render_fleet_preview(config: FleetConfig) -> str:
    """Render a human-readable preview of a FleetConfig.

    Derived MECHANICALLY from the validated ``FleetConfig`` object — never from
    prose or agent paraphrase. The human approves this output, not the agent's
    summary of it, which is why it must be complete and exact. Every
    fleet-controlled string is passed through ``_sanitize`` so a tampered fleet
    cannot use terminal escapes to spoof or hide any part of the preview.

    Returns a multi-line string suitable for terminal display.
    """
    out = [f"=== fleet preview: {_sanitize(config.name)} ==="]

    # --- synthesizer (cost predicate runs on the RAW strings; only display is sanitized) ---
    synth_str = f"{_sanitize(config.synthesis.provider)}/{_sanitize(config.synthesis.model)}"
    out.append(f"\nSynthesizer: {synth_str}")
    if _looks_api_billed(config.synthesis.provider, config.synthesis.model):
        out.append("  ⚠ bills at API rates inside Hermes")

    # --- privileged tools flag (our text, not fleet-controlled — cannot be spoofed) ---
    if config.allow_privileged_tools:
        out.append("\n⚠ PRIVILEGED TOOLS ENABLED (allow_privileged_tools: true)")
    else:
        out.append("\nallow_privileged_tools: false")

    # --- synthesis prompt (multi-line: newlines preserved, other controls stripped) ---
    raw_prompt = config.synthesis.prompt.strip() if config.synthesis.prompt else ""
    prompt_text = _sanitize(raw_prompt, multiline=True) if raw_prompt else "(none)"
    out.append(f"\nSynthesis prompt:\n  {prompt_text.replace(chr(10), chr(10) + '  ')}")

    # --- specialists ---
    out.append(f"\nSpecialists ({len(config.specialists)}):")
    for s in config.specialists:
        toolset_str = ", ".join(_sanitize(t) for t in s.toolset) if s.toolset else "(none)"
        out.append(f"  [{_sanitize(s.role)}]  {_sanitize(s.provider)}/{_sanitize(s.model)}  toolset={toolset_str}")
        if s.focus:
            out.append(f"    focus: {_sanitize(s.focus)}")

    out.append("\n=== end preview ===")
    return "\n".join(out)


def render_result(result: FleetResult) -> str:
    header = "synthesized result" if result.ok else "partial result (no synthesis)"
    out = [f"=== {_sanitize(result.fleet)} — {header} ==="]
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
            out.append(f"\n--- {_sanitize(r.role)} ({_sanitize(r.provider)}/{_sanitize(r.model)}) ---\n{r.text}")
    out.append("\n--- provenance ---")
    # Sanitize only the CONFIG-derived identity fields (fleet/role/provider/model)
    # so a tampered fleet can't forge provenance rows. Model output (r.text/r.error,
    # result.synthesis) is deliberately NOT stripped here — that's the deferred
    # injection->terminal chain (GH #5), not this surface's job.
    for r in result.specialists:
        if r.ok:
            tag = "ok  "
        elif r.timed_out:
            tag = "TIMEOUT"
        else:
            tag = "FAIL"
        suffix = "" if r.ok else f": {r.error}"
        out.append(f"[{tag}] {_sanitize(r.role)} ({_sanitize(r.provider)}/{_sanitize(r.model)}){suffix}")
    if result.notes:
        out.append("\nnotes:")
        out.extend(f"  - {n}" for n in result.notes)
    return "\n".join(out)
