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
from cadre.preview_lint import load_palette, off_palette_model_pairs
from cadre.text_safety import sanitize as _sanitize


def preflight_refusal(cfg: FleetConfig, *, palette_path: str | Path | None = None) -> str | None:
    """Return a refusal message when ``cfg`` has an off-palette model, else ``None``.

    Loads the palette via ``preview_lint.load_palette`` — ``palette_path`` →
    the ``CADRE_PALETTE`` env var → the default ``~/.cadre/palette.yaml``
    (``palette_path`` is an explicit-injection seam for tests/callers; both
    runners call this with no argument, matching the plan's single-``cfg``
    signature and getting the env/default resolution).

    Degrades OPEN — matching ``render_preview_warnings``'s own posture —
    when the palette is absent or malformed (``load_palette`` returns
    ``None``): a fleet with no verified palette to check against proceeds.
    Also returns ``None`` when a palette IS present but every specialist,
    synthesizer, and judge model is on it.

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
