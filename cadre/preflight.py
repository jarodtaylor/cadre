"""#62 preflight-refuse: block an off-palette fleet before any model call.

Caller-layer only: imported by ``cadre/cli.py`` and ``cadre/data/skill/run.py``.
NEVER imported by ``engine.py`` or ``model_client.py`` (R7) — the engine stays
a pure, palette-free computation; this gate is read-only config inspection
performed by the two runners *before* they spend anything (KTD5;
``tests/test_personas.py``'s ``TestEngineIsolation`` guards this in both
directions, mirroring ``approval.py``'s posture).

Built on ``preview_lint.off_palette_model_pairs`` — the exact structured
membership check ``check_palette`` already uses for its preview warnings
(KTD4) — so this gate and the preview warnings share one source and can never
disagree about what counts as off-palette. Scope is deliberately narrow (R4):
refuses ONLY on an off-palette *model* (a specialist, the synthesizer, or the
judge); off-palette *toolsets* stay a warning, never a refusal, and no palette
present at all degrades OPEN (proceed) — matching the preview's own posture.
Requiring a palette to exist is #61's job, not this gate's.
"""

from __future__ import annotations

from pathlib import Path

from cadre.config import FleetConfig
from cadre.preview_lint import load_palette, off_palette_model_pairs, resolve_palette_path
from cadre.text_safety import sanitize as _sanitize


def preflight_refusal(cfg: FleetConfig, *, palette_path: str | Path | None = None) -> str | None:
    """Return a refusal message when ``cfg`` has an off-palette model, else ``None``.

    Loads the palette via ``preview_lint.load_palette`` — ``palette_path`` →
    the ``CADRE_PALETTE`` env var → the default ``~/.cadre/palette.yaml``
    (``palette_path`` is an explicit-injection seam for tests/callers; both
    runners call this with no argument, matching the plan's single-``cfg``
    signature and getting the env/default resolution).

    Degrades OPEN — returns ``None`` — when the palette is genuinely ABSENT
    (the operator opted out of palette checking): a fleet with no palette to
    check against proceeds. But a palette that is PRESENT yet unreadable or
    malformed (``load_palette`` returns ``None`` while the resolved file
    exists) FAILS CLOSED with a refusal — a broken palette must not silently
    disable the #62 spend-gate (correctness DevEx: fail loud on a bad config,
    do not spend ungated). Also returns ``None`` when a palette IS present and
    valid but every specialist, synthesizer, and judge model is on it.

    Otherwise returns a clear, multi-line refusal naming each offending role
    + ``(provider, model)`` — every field ``_sanitize``d, since the fleet
    YAML is a possibly-tampered trust surface (the same rule the rest of
    ``preview_lint`` follows) — plus a fix hint. This is the run-time #62
    gate: it frames the ``FailureReason.OFF_PALETTE`` condition, but (per
    KTD3) a preflight refusal writes no manifest, so the caller's distinct
    ``ExitCode.PREFLIGHT_REFUSE`` exit code — not this string's content — is
    the structured signal an agent operator branches on. Off-palette
    *toolsets* are never refused here (R4) — this checks models only.
    """
    palette = load_palette(palette_path)
    if palette is None:
        # A genuinely-absent palette degrades open (operator opted out). But a
        # PRESENT-but-unreadable/malformed palette must NOT silently disable the
        # spend-gate: fail closed with a clear error. Stat the resolved path to
        # tell the two apart (load_palette collapses both to None). Guard the
        # stat itself so a stat failure degrades open rather than crashing.
        resolved = resolve_palette_path(palette_path)
        present = False
        if resolved is not None:
            try:
                present = resolved.exists()
            except (OSError, ValueError):
                present = False
        if present:
            return (
                "Refused (unreadable palette) — no spend has occurred. The host "
                f"palette at {_sanitize(str(resolved))} exists but could not be read "
                "or parsed (missing keys, bad YAML, or malformed model entries), so "
                "off-palette models cannot be checked.\n"
                "Fix: repair or remove the palette, or run `cadre verify-palette` "
                "to regenerate it."
            )
        return None

    off_palette = off_palette_model_pairs(cfg, palette)
    if not off_palette:
        return None

    lines = [
        "Refused (off-palette model) — no spend has occurred. "
        "The following model(s) are not on the host palette:",
    ]
    for role_label, provider, model in off_palette:
        lines.append(f"  - {_sanitize(role_label)}: ({_sanitize(provider)}, {_sanitize(model)})")
    lines.append(
        "Fix: swap each offending pair to a verified entry in ~/.cadre/palette.yaml, "
        "or run `cadre verify-palette` to (re-)verify your authenticated providers."
    )
    return "\n".join(lines)
