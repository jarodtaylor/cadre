"""Caller-layer palette and focus validation for fleet previews.

Caller-layer only: imported by ``skills/cadre-fleet/run.py`` and
``fleet_engine/cli.py``. NEVER imported by ``engine.py``, ``model_client.py``,
or ``config.py`` (R8). Those modules must stay palette-free so the engine
remains a pure, host-agnostic computation.

The palette (``~/.cadre/palette.yaml``) is a timestamped snapshot of verified
(provider, model) pairs and safe toolsets on the host. An absent pair may still
resolve at runtime — this module warns, never blocks.

Focus lint (``check_focus_grounding``) is palette-independent: it checks
retrieval-toolset lanes for the absence of a sourcing directive. A focus that
demands sources grounds the lane even when anti-grounding phrasing is also
present (e.g. "breadth over depth" + "cite a primary source with a link" → no
warn). See ``docs/solutions/design-patterns/specialist-focus-grounding-control.md``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from fleet_engine.config import FleetConfig
from fleet_engine.render import _sanitize  # caller-layer sibling; render does not import this module (no cycle)

# Default palette location — mirrors the CADRE_RUN_DIR convention in capture.py.
DEFAULT_PALETTE_PATH = "~/.cadre/palette.yaml"


# ---------------------------------------------------------------------------
# Palette dataclass
# ---------------------------------------------------------------------------


@dataclass
class Palette:
    """Verified host palette: a set of (provider, model) pairs and toolset names."""

    models: set[tuple[str, str]] = field(default_factory=set)
    toolsets: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# I/O: load the palette (the ONLY I/O in this module)
# ---------------------------------------------------------------------------


def load_palette(path: str | Path | None = None) -> Optional[Palette]:
    """Load and parse the host palette YAML.

    Path resolution order:
    1. ``path`` parameter (explicit injection — used by tests and CI).
    2. ``CADRE_PALETTE`` env var, if set (dev-host testability seam, mirrors
       ``CADRE_RUN_DIR`` in ``capture.py``).
    3. ``DEFAULT_PALETTE_PATH`` (``~/.cadre/palette.yaml``).

    Returns ``None`` — never raises — on:
    - Missing or unreadable file.
    - YAML parse error or encoding error.
    - Non-mapping top-level YAML.
    - Missing or non-list ``models``/``toolsets`` keys.
    - Any malformed entry in ``models`` (partial/garbage palette → None, not
      a partial result, so a corrupted palette degrades cleanly rather than
      producing a confusingly incomplete check).
    """
    if path is None:
        env = os.getenv("CADRE_PALETTE")
        path = env if env else DEFAULT_PALETTE_PATH

    try:
        # Path()/expanduser() can raise ValueError (an embedded NUL byte in the path) or
        # TypeError (a non-str/Path) — both must degrade to None, not crash, to honor the
        # never-raises contract. read_text adds OSError (missing/unreadable) and
        # UnicodeDecodeError (a non-UTF-8 / binary palette, NOT an OSError subclass).
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None

    models_raw = data.get("models")
    if not isinstance(models_raw, list):
        return None

    toolsets_raw = data.get("toolsets")
    if not isinstance(toolsets_raw, list):
        return None

    # Build the models set — any malformed entry degrades the whole palette to None.
    models: set[tuple[str, str]] = set()
    for entry in models_raw:
        if not isinstance(entry, dict):
            return None
        provider = entry.get("provider")
        model = entry.get("model")
        if provider is None or model is None:
            return None
        # Coerce to str so YAML int/float values compare stably with fleet strings.
        models.add((str(provider), str(model)))

    toolsets: set[str] = set()
    for t in toolsets_raw:
        if not isinstance(t, str):
            return None
        toolsets.add(t)

    return Palette(models=models, toolsets=toolsets)


# ---------------------------------------------------------------------------
# Pure check (no I/O)
# ---------------------------------------------------------------------------


def check_palette(config: FleetConfig, palette: Palette) -> list[str]:
    """Return a list of warning strings for off-palette models/toolsets.

    Pure (no I/O). Assumes ``palette`` is a valid, non-None ``Palette``.

    Model check:
    - Each specialist's ``(provider, model)`` pair is checked against
      ``palette.models``.
    - The synthesizer is checked ONLY when ``config.convergence != "collect"``
      AND ``config.synthesis is not None``. Collect fleets have no synthesizer
      role, so we never inspect ``config.synthesis`` in collect mode.

    Toolset check:
    - Each specialist's toolset entries are checked against ``palette.toolsets``.
    - The synthesizer has no toolset and is never checked here.

    Each warning names the role and the off-palette value and includes a
    concrete swap hint so the first-run operator sees it as a guided task,
    not noise.
    """
    warnings: list[str] = []
    palette_hint = "swap to a verified pair from ~/.cadre/palette.yaml"

    # These warnings print to the --preview / validate approval surface, so every
    # fleet-controlled string is _sanitize()d before interpolation — the same invariant
    # render.py enforces (a tampered fleet must not spoof or hide the approval output via
    # terminal escapes). See docs/solutions/design-patterns/
    # sanitize-trust-surface-renders-against-terminal-escapes.md. Membership checks use the
    # RAW values; only the displayed text is sanitized.
    for spec in config.specialists:
        pair = (spec.provider, spec.model)
        if pair not in palette.models:
            warnings.append(
                f"specialist '{_sanitize(spec.role)}': "
                f"({_sanitize(spec.provider)}, {_sanitize(spec.model)}) not in palette; "
                f"{palette_hint}"
            )
        for tool in spec.toolset:
            if tool not in palette.toolsets:
                warnings.append(
                    f"specialist '{_sanitize(spec.role)}': toolset '{_sanitize(tool)}' not in palette; "
                    f"verify the toolset name against ~/.cadre/palette.yaml"
                )

    # Synthesizer: only for synthesize-convergence fleets with a non-None synthesis.
    if config.convergence != "collect" and config.synthesis is not None:
        syn = config.synthesis
        if (syn.provider, syn.model) not in palette.models:
            warnings.append(
                f"synthesizer: ({_sanitize(syn.provider)}, {_sanitize(syn.model)}) not in palette; "
                f"{palette_hint}"
            )

    # Judge: only for judge-convergence fleets. The judge is a model call, so
    # palette-validate it like the synthesizer.
    if config.convergence == "judge" and config.judge is not None:
        j = config.judge
        if (j.provider, j.model) not in palette.models:
            warnings.append(
                f"judge: ({_sanitize(j.provider)}, {_sanitize(j.model)}) not in palette; "
                f"{palette_hint}"
            )

    return warnings


# ---------------------------------------------------------------------------
# Focus-grounding lint (pure, no I/O) — U6
# ---------------------------------------------------------------------------

# Toolsets that perform live retrieval; a lane with these is subject to focus
# grounding checks. web/search/x_search are in config.py's SAFE_TOOLSETS; exa and
# firecrawl are NOT (yet) — a fleet declaring them needs allow_privileged_tools and is
# rejected by config validation before this lint runs, so those two entries are reachable
# only for privileged fleets. They stay listed so the lint already covers them if/when
# they are host-verified into SAFE_TOOLSETS. (Names per the grounding-control learning;
# not re-verified against hermes-agent here — see AGENTS.md.)
RETRIEVAL_TOOLSETS = frozenset({"web", "search", "x_search", "exa", "firecrawl"})

# Stems that indicate a sourcing directive in the focus text. Matched with a LEADING
# word boundary (\b + stem) against the lowercased focus, so morphological variants still
# count ("source"->sources/sourced, "cite"->cited/cites, "attribut"->attribute/attribution)
# while incidental substrings do NOT ("resource" no longer satisfies "source"). "citation"
# is listed separately since it does not share the "cite" stem. The harmful direction is a
# MISSED warn — a truly ungrounded retrieval lane slipping through — so the match aims to
# recognize genuine sourcing language precisely, not loosely.
_SOURCING_TERMS = ("cite", "citation", "source", "link", "url", "primary source", "reference", "attribut")

# Substrings that indicate anti-grounding intent (breadth/speed framing that
# suppresses deep retrieval). Only consulted when NO sourcing term is present,
# to choose the more specific warning copy.
_ANTI_GROUNDING_TERMS = ("fast", "broad", "quick scan", "breadth over depth")

# Appended to every focus-lint warning: the profile-scoped-tool caveat (KTD6).
# Even a well-written focus runs ungrounded if the toolset isn't provisioned
# in the active Hermes profile. See docs/RUNBOOK.md and
# docs/solutions/integration-issues/hermes-tools-are-profile-scoped.md.
_PROFILE_CAVEAT = (
    "also ensure the toolset is provisioned in the active Hermes profile "
    "(tools are profile-scoped — see docs/RUNBOOK.md)"
)


def check_focus_grounding(config: "FleetConfig") -> list[str]:
    """Return warnings for retrieval-toolset lanes whose focus lacks sourcing language.

    Pure (no I/O). A retrieval lane is any specialist whose toolset intersects
    ``RETRIEVAL_TOOLSETS``. Lanes outside that set are not checked.

    Warn logic (priority order):
    1. If the lane's focus contains any ``_SOURCING_TERMS`` substring → grounded,
       no warn (even when anti-grounding phrasing is also present).
    2. If the lane's focus contains any ``_ANTI_GROUNDING_TERMS`` substring →
       warn with the anti-grounding-specific copy (more urgent).
    3. Otherwise → warn with the generic "no sourcing directive" copy.

    Each warning includes an actionable fix and the profile-scoped-tool caveat.
    Warn-never-block (KTD5): always returns a list, never raises.
    """
    warnings: list[str] = []
    for spec in config.specialists:
        if not (set(spec.toolset) & RETRIEVAL_TOOLSETS):
            continue  # not a retrieval lane → skip
        focus = (spec.effective_instruction or "").lower()
        if any(re.search(r"\b" + re.escape(t), focus) for t in _SOURCING_TERMS):
            continue  # sourcing directive present → grounded → no warn
        # Retrieval lane with NO sourcing directive. Role is _sanitize()d — these
        # warnings print to the approval surface (see the note in check_palette).
        if any(t in focus for t in _ANTI_GROUNDING_TERMS):
            msg = (
                f"specialist '{_sanitize(spec.role)}': retrieval toolset + anti-grounding phrasing "
                f"(breadth/speed framing) but no sourcing directive — "
                f"add \"cite a primary source with a link per claim\" to the specialist's instruction; {_PROFILE_CAVEAT}"
            )
        else:
            msg = (
                f"specialist '{_sanitize(spec.role)}': retrieval toolset but its instruction does not request "
                f"sources or citations — "
                f"add \"cite a primary source with a link per claim\" to the specialist's instruction; {_PROFILE_CAVEAT}"
            )
        warnings.append(msg)
    return warnings


# ---------------------------------------------------------------------------
# Wiring helper: load + format (used by run.py and cli.py)
# ---------------------------------------------------------------------------


def render_preview_warnings(
    config: FleetConfig,
    palette_path: str | Path | None = None,
) -> str:
    """Load palette and format a validation summary for the fleet preview.

    Returns a single string suitable for printing directly to stdout as part
    of the preview the human approves.

    When the palette is absent/unreadable, returns a "validation skipped" note
    so the operator knows to run ``write_palette`` before a production run.

    Structure: focus-lint warnings (``check_focus_grounding``) are merged into
    the SAME ``⚠ fleet validation`` block as palette warnings and run regardless
    of whether the palette loaded (focus lint is palette-independent).
    """
    # Resolve the path for the "skipped" note (the user sees the path we tried).
    # Path()/expanduser() can raise ValueError/TypeError on a malformed path arg (e.g. an
    # embedded NUL byte); fall back to the raw value rather than crash the preview
    # (warn-never-block). It is _sanitize()d at the interpolation site below.
    raw_path = palette_path if palette_path is not None else (os.getenv("CADRE_PALETTE") or DEFAULT_PALETTE_PATH)
    try:
        resolved_path = str(Path(raw_path).expanduser())
    except (ValueError, TypeError):
        resolved_path = str(raw_path)

    palette = load_palette(palette_path)

    focus_warnings: list[str] = check_focus_grounding(config)

    if palette is None:
        skipped_line = (
            # Sanitize the env/caller-derived path: CADRE_PALETTE can carry a newline or
            # terminal escape that would otherwise forge a fake validation line on the
            # approval surface (the same invariant render.py enforces for CADRE_RUN_DIR
            # paths in breadcrumbs).
            f"palette validation skipped — no palette at {_sanitize(str(resolved_path))}; "
            "run `spikes/verify_aiagent_providers.py write_palette` to generate one"
        )
        # U6: if focus_warnings, prepend a ⚠ block for focus-lint even without a palette.
        if focus_warnings:
            lines = ["⚠ fleet validation:"]
            lines.extend(f"  - {w}" for w in focus_warnings)
            lines.append(f"  - {skipped_line}")
            return "\n".join(lines)
        return skipped_line

    all_warnings = check_palette(config, palette) + focus_warnings

    if not all_warnings:
        return "✓ fleet validation: all models/toolsets on palette"

    n = len(all_warnings)
    lines = [f"⚠ fleet validation — {n} warning(s):"]
    lines.extend(f"  - {w}" for w in all_warnings)
    return "\n".join(lines)
