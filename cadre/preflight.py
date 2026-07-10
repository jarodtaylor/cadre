"""#62/#78 preflight-refuse: block an off-palette, policy-blocked, or
palette-less fleet before any model call.

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

#78 policy gate: BEFORE the palette check, every model-bearing pair (every
specialist, plus the synthesizer/judge when convergence makes them
model-bearing — the exact same set ``off_palette_model_pairs`` already
enumerates, reused here against an empty ``Palette()`` sentinel so both
checks can never drift on "which roles carry a model") is checked against the
local policy gate (``cadre.policy``, ``~/.cadre/policy.yaml``). A banned pair
refuses even when it IS on the palette — the policy gate is a separate,
independently-tightenable control, not palette membership (defense in
depth). Checked first because it is the harder veto: a palette fix cannot
un-block a policy-banned pair, but a policy fix can never widen what the
palette already restricts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from cadre.config import FleetConfig
from cadre.discover import default_candidates_path
from cadre.failure import FailureReason
from cadre.policy import PolicyError, default_policy_path, load_policy
from cadre.preview_lint import Palette, load_palette, off_palette_model_pairs, resolve_palette_path
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


def _candidates_file_exists() -> bool:
    """Best-effort presence probe for the candidates file the remedy names.

    Selects ONLY the absent-palette remedy text below — when a (possibly
    hand-curated) ``~/.cadre/palette-candidates.yaml`` already exists, the
    remedy must point at `cadre verify-palette` directly, never at
    `cadre discover`, which would REGENERATE the file and discard those edits
    (the refusal text is a recipe an agent follows verbatim, and the skill
    authorizes self-running discover exactly when this refusal names it).
    Same patchable-seam pattern as ``_hermes_cli_available`` — tests force it
    both ways because a provisioned host genuinely has the file. Any probe
    failure degrades to False (the remedies that assume no file always work:
    discover refuses create-or-overwrite loudly, and the manual edit path is
    self-evident once the operator looks).
    """
    try:
        return default_candidates_path().expanduser().exists()
    except Exception:  # noqa: BLE001 — any probe failure -> the no-file remedies
        return False


def _all_model_pairs(cfg: FleetConfig) -> list[tuple[str, str, str]]:
    """Every ``(role_label, provider, model)`` triple in ``cfg``, unconditionally.

    Reuses ``off_palette_model_pairs`` against an empty ``Palette()``
    sentinel: nothing is ever a member of an empty set, so every
    model-bearing role registers as "off" that empty palette — i.e. this
    returns literally every specialist/synthesizer/judge model-bearing pair,
    regardless of any real palette. Reusing it (rather than re-deriving the
    specialist/synthesizer/judge convergence-mode gating a second time) means
    the policy check below can never drift from the palette check on "which
    roles carry a model" (the same DRY argument ``off_palette_model_pairs``
    already makes for ``check_palette`` vs. this module).
    """
    return off_palette_model_pairs(cfg, Palette())


def preflight_refusal(
    cfg: FleetConfig,
    *,
    palette_path: str | Path | None = None,
    policy_path: str | Path | None = None,
) -> str | None:
    """Return a refusal message when ``cfg`` is policy-blocked or has an
    off-palette model, else ``None``.

    Two independent gates, checked in order — either can refuse on its own:

    1. **Policy (#78).** Loads the policy via ``cadre.policy.load_policy`` —
       ``policy_path`` -> the ``CADRE_POLICY`` env var -> the default
       ``~/.cadre/policy.yaml`` (``policy_path`` is an explicit-injection seam
       for tests/callers, mirroring ``palette_path`` below; both runners call
       this with no keyword arguments and get the env/default resolution). A
       malformed policy file (``PolicyError``) fails CLOSED with its own
       refusal — a broken safety file must never silently mean no safety.
       Every model-bearing pair (``_all_model_pairs``) is checked against the
       loaded policy; a banned pair refuses even when it IS on the palette
       (the policy gate is independent of, and checked before, palette
       membership — defense in depth).
    2. **Palette (#62/#61).** Loads the palette via ``preview_lint.load_palette``
       — ``palette_path`` -> the ``CADRE_PALETTE`` env var -> the default
       ``~/.cadre/palette.yaml``. Refuses — never degrades open — when the
       palette is genuinely ABSENT (#61/#62 flip, KTD7): a host with nothing
       to check against now gets a legible refusal instead of silently
       running every model ungated. The remedy it names depends on whether
       Hermes's CLI is importable on this host (``_hermes_cli_available``):
       ``cadre discover`` when it is, else the manual
       ``~/.cadre/palette-candidates.yaml`` hand-edit. A palette that is
       PRESENT yet unreadable or malformed (``load_palette`` returns ``None``
       while the resolved file exists) ALSO fails CLOSED with a refusal, as
       before this flip.

    Returns ``None`` only when the policy gate blocks nothing AND a palette
    IS present and valid AND every specialist, synthesizer, and judge model
    is on it.

    Every refusal names the offending role + ``(provider, model)`` —
    every field ``_sanitize``d, since the fleet YAML is a possibly-tampered
    trust surface (the same rule the rest of ``preview_lint`` follows) — plus
    a fix hint. This is a run-time gate: it frames the
    ``FailureReason.POLICY_BLOCKED`` / ``FailureReason.OFF_PALETTE``
    conditions (each refusal embeds the matching reason's ``.value`` as a
    text token), but (per KTD3) a preflight refusal writes no manifest, so
    the caller's distinct ``ExitCode.PREFLIGHT_REFUSE`` exit code — not this
    string's content — is the structured signal an agent operator branches
    on. Off-palette *toolsets* are never refused here (R4) — this checks
    models only.
    """
    resolved_policy_path = (
        Path(policy_path).expanduser()
        if policy_path is not None
        else Path(default_policy_path()).expanduser()
    )
    try:
        policy = load_policy(resolved_policy_path)
    except PolicyError as exc:
        return (
            f"Refused ({FailureReason.POLICY_BLOCKED.value}) — no spend has "
            f"occurred. The policy file could not be trusted: {_sanitize(str(exc))}\n"
            f"Fix: repair or remove {_sanitize(str(resolved_policy_path))}."
        )

    policy_violations = []
    for role_label, provider, model in _all_model_pairs(cfg):
        violation = policy.check(provider, model)
        if violation is not None:
            policy_violations.append((role_label, violation))
    if policy_violations:
        lines = [
            f"Refused ({FailureReason.POLICY_BLOCKED.value}) — no spend has "
            "occurred. The following model(s) are blocked by policy "
            f"({_sanitize(str(resolved_policy_path))}):",
        ]
        for role_label, v in policy_violations:
            lines.append(
                f"  - {_sanitize(role_label)}: ({_sanitize(v.provider)}, "
                f"{_sanitize(v.model)}) — {_sanitize(v.rule)}"
            )
        lines.append(
            f"Fix: edit {_sanitize(str(resolved_policy_path))}, or swap each "
            "offending pair for one the policy allows."
        )
        return "\n".join(lines)

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
        if _candidates_file_exists():
            # A candidates file already exists (possibly hand-curated) — the
            # remedy must NOT name `cadre discover`, which would regenerate it
            # and discard those edits (Codex adversarial catch: the refusal is
            # a recipe an agent follows verbatim, and the skill authorizes it
            # to self-run discover when named here).
            fix = (
                "Fix: run `cadre verify-palette` — your existing "
                "~/.cadre/palette-candidates.yaml will be verified as-is."
            )
        elif _hermes_cli_available():
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
