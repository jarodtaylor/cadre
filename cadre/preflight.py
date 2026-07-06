"""#62 preflight-refuse: block an off-palette (or palette-less) fleet before
any model call.

Caller-layer only: imported by ``cadre/cli.py`` and ``cadre/data/skill/run.py``.
NEVER imported by ``engine.py`` or ``model_client.py`` (R7) — the engine stays
a pure, palette-free computation; this gate is read-only config inspection
performed by the two runners *before* they spend anything (KTD5;
``tests/test_personas.py``'s ``TestEngineIsolation`` guards this in both
directions, mirroring ``approval.py``'s posture).

Built on ``preview_lint.off_palette_model_pairs`` — the exact structured
membership check ``check_palette`` already uses for its preview warnings
(KTD4) — so this gate and the preview warnings share one source and can never
disagree about what counts as off-palette. Scope is deliberately narrow (R4)
on the model-vs-toolset axis: refuses on an off-palette *model* (a specialist,
the synthesizer, or the judge); off-palette *toolsets* stay a warning, never a
refusal — matching the preview's own posture there.

A genuinely-absent palette is NOT exempt (#61/#62 flip, KTD7 — this used to
degrade OPEN, i.e. proceed, before ``cadre discover``/#61 existed to give a
fresh host an easy fix): a host with no ``~/.cadre/palette.yaml`` at all now
refuses too, naming a remedy that works on THIS host (``cadre discover`` when
Hermes's CLI is importable, else the manual ``palette-candidates.yaml``
hand-edit — see ``_hermes_cli_available``) rather than silently running every
model ungated. A PRESENT-but-unreadable/malformed palette was already a
refusal before this flip and is unchanged by it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from cadre.config import FleetConfig
from cadre.preview_lint import load_palette, off_palette_model_pairs, resolve_palette_path
from cadre.text_safety import sanitize as _sanitize


def _hermes_cli_available() -> bool:
    """Best-effort PRESENCE probe for Hermes's CLI package.

    Selects ONLY which remedy the absent-palette refusal below names — it
    never gates the refusal itself (that fires unconditionally once the
    palette is absent). A presence check via ``importlib.util.find_spec``
    ONLY: it never imports or executes ``hermes_cli`` (mirrors
    ``cadre.discover``'s own presence-vs-import split — discovery itself
    lazy-imports ``hermes_cli.inventory`` only inside its ``_fetch_inventory``,
    at call time, never here).

    Spoofable by cwd/``PYTHONPATH`` (a stale entry could make this return
    True or False for the wrong reason) — accepted, since a wrong guess only
    picks the wrong remedy TEXT and self-corrects: `cadre discover` itself
    re-validates for real and fails closed naming the manual fallback if it
    turns out Hermes wasn't actually available. Any exception during the
    probe (a corrupted meta-path finder, a stale
    ``sys.modules["hermes_cli"] = None`` left by a prior failed import,
    anything) also degrades to ``False`` — the conservative remedy that
    always works regardless of what's actually installed.
    """
    try:
        return importlib.util.find_spec("hermes_cli") is not None
    except Exception:  # noqa: BLE001 — any probe failure -> the conservative remedy
        return False


def preflight_refusal(cfg: FleetConfig, *, palette_path: str | Path | None = None) -> str | None:
    """Return a refusal message when ``cfg`` has an off-palette model, else ``None``.

    Loads the palette via ``preview_lint.load_palette`` — ``palette_path`` →
    the ``CADRE_PALETTE`` env var → the default ``~/.cadre/palette.yaml``
    (``palette_path`` is an explicit-injection seam for tests/callers; both
    runners call this with no argument, matching the plan's single-``cfg``
    signature and getting the env/default resolution).

    Refuses — never degrades open — when the palette is genuinely ABSENT
    (#61/#62 flip, KTD7): a host with nothing to check against now gets a
    legible refusal instead of silently running every model ungated. The
    remedy it names depends on whether Hermes's CLI is importable on this
    host (``_hermes_cli_available``): ``cadre discover`` when it is, else the
    manual ``~/.cadre/palette-candidates.yaml`` hand-edit. A palette that is
    PRESENT yet unreadable or malformed (``load_palette`` returns ``None``
    while the resolved file exists) ALSO fails CLOSED with a refusal, as
    before this flip — a broken palette must not silently disable the #62
    spend-gate either (correctness DevEx: fail loud on a bad or missing
    config, do not spend ungated). Returns ``None`` only when a palette IS
    present and valid and every specialist, synthesizer, and judge model is
    on it.

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
        # Both branches below now refuse -- but for two different reasons, so
        # tell them apart first. A PRESENT-but-unreadable/malformed palette
        # fails closed with a "fix your palette" error (unchanged by this
        # unit). A genuinely-ABSENT palette used to degrade open; it now ALSO
        # fails closed, but with a "get a palette" remedy instead (KTD7). Stat
        # the resolved path to tell the two apart (load_palette collapses both
        # to None). Guard the stat itself so a stat failure is treated as
        # absent rather than crashing.
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
        # Genuinely absent (#61/#62 flip, KTD7): refuse, naming whichever remedy
        # actually works on this host. The location is included when resolvable,
        # sanitized like every other dynamic field this gate renders (CADRE_PALETTE
        # is a caller-set env var — a trust surface, not user-typed-and-trusted).
        location = f" at {_sanitize(str(resolved))}" if resolved is not None else ""
        if _hermes_cli_available():
            fix = (
                "Fix: run `cadre discover` to auto-discover your authenticated "
                "providers, then `cadre verify-palette` to confirm them on this host."
            )
        else:
            fix = (
                "Fix: edit ~/.cadre/palette-candidates.yaml with your authenticated "
                "providers and models, then run `cadre verify-palette` to confirm "
                "them on this host."
            )
        return (
            "Refused (no palette) — no spend has occurred. No host palette was "
            f"found{location}, so off-palette models cannot be checked.\n"
            f"{fix}"
        )

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
