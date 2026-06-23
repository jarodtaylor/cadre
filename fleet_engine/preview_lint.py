"""Caller-layer palette and focus validation for fleet previews.

Caller-layer only: imported by ``skills/cadre-fleet/run.py`` and
``fleet_engine/cli.py``. NEVER imported by ``engine.py``, ``model_client.py``,
or ``config.py`` (R8). Those modules must stay palette-free so the engine
remains a pure, host-agnostic computation.

The palette (``~/.cadre/palette.yaml``) is a timestamped snapshot of verified
(provider, model) pairs and safe toolsets on the host. An absent pair may still
resolve at runtime — this module warns, never blocks.

U6 seam: ``render_preview_warnings`` is structured so focus-lint warnings
(``check_focus_grounding``, to be added in U6) can be appended into the SAME
``⚠ fleet validation`` header block. The U6 hook runs even when the palette is
None (focus lint is palette-independent). See the comment in
``render_preview_warnings`` marking the seam.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from fleet_engine.config import FleetConfig

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
    resolved = Path(path).expanduser()

    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        data = yaml.safe_load(raw)
    except (yaml.YAMLError, UnicodeDecodeError):
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

    for spec in config.specialists:
        pair = (spec.provider, spec.model)
        if pair not in palette.models:
            warnings.append(
                f"specialist '{spec.role}': ({spec.provider}, {spec.model}) not in palette; "
                f"{palette_hint}"
            )
        for tool in spec.toolset:
            if tool not in palette.toolsets:
                warnings.append(
                    f"specialist '{spec.role}': toolset '{tool}' not in palette; "
                    f"verify the toolset name against ~/.cadre/palette.yaml"
                )

    # Synthesizer: only for synthesize-convergence fleets with a non-None synthesis.
    if config.convergence != "collect" and config.synthesis is not None:
        syn = config.synthesis
        if (syn.provider, syn.model) not in palette.models:
            warnings.append(
                f"synthesizer: ({syn.provider}, {syn.model}) not in palette; "
                f"{palette_hint}"
            )

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

    Structure note (U6 seam): focus-lint warnings (``check_focus_grounding``)
    will be added here in U6. They belong in the SAME ``⚠ fleet validation``
    block and run regardless of whether the palette loaded. The seam is the
    ``focus_warnings`` list below — U6 populates it before the header is built.
    """
    # Resolve the path (for the "skipped" note — the user sees the path we tried).
    if palette_path is None:
        env = os.getenv("CADRE_PALETTE")
        resolved_path = Path(env if env else DEFAULT_PALETTE_PATH).expanduser()
    else:
        resolved_path = Path(palette_path).expanduser()

    palette = load_palette(palette_path)

    # --- U6 seam: populate focus_warnings here when check_focus_grounding is added ---
    focus_warnings: list[str] = []
    # focus_warnings = check_focus_grounding(config)  # U6 adds this line

    if palette is None:
        skipped_line = (
            f"palette validation skipped — no palette at {resolved_path}; "
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
