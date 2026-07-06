"""Shared test fixture: a palette.yaml that matches a fleet's declared models.

#61/#62 interaction: ``cadre.preflight.preflight_refusal`` refuses a fleet
whose specialist/synthesizer/judge model is absent from the host palette
(``~/.cadre/palette.yaml`` -> ``CADRE_PALETTE`` env -> the test suite's
guaranteed-absent hermetic default; see ``tests/__init__.py``). Before the U7
flip, an ABSENT palette degrades open (proceed); U7 flips that default to a
refusal. Any fleet-RUN test that does not itself exercise the preflight gate
(``tests/test_cli.py``'s ``TestRunCommandPreflightRefuse`` /
``TestSkillPreflightRefuse``) needs an explicit palette covering every model
the fleet under test declares -- otherwise, post-flip, it would either start
failing outright, or start passing for the WRONG reason (a preflight refusal
silently masking the behavior the test actually means to exercise, e.g. a
read-only-dir fail-fast check that preflight would now short-circuit before
ever reaching).

``matching_palette`` writes that palette to a temp file and patches
``CADRE_PALETTE`` to it, so ``preflight_refusal`` sees "every declared model is
on the palette" (returns ``None`` -- proceed) regardless of whether the
absent-palette default is open or closed. Reused by ``tests/test_cli.py`` and
``tests/test_capture.py`` -- the two fleet-run entry points (``cadre run`` /
the Hermes skill's ``run.py``).

Deliberately independent of ``tests/test_cli.py`` (no import either
direction) so both consumers depend on one neutral module rather than on each
other.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from cadre.config import FleetConfig


def _pairs_for_config(cfg: FleetConfig) -> set[tuple[str, str]]:
    """Every (provider, model) pair ``preflight_refusal`` would check for
    ``cfg`` -- specialists, plus the synthesizer/judge ONLY when the
    convergence mode makes that role model-bearing. Mirrors
    ``cadre.preview_lint.off_palette_model_pairs``'s own gating exactly (the
    same source both the #62 gate and the preview warnings share), so a
    palette built from this set can never leave a real model-bearing role
    unchecked.
    """
    pairs = {(s.provider, s.model) for s in cfg.specialists}
    if cfg.convergence == "synthesize" and cfg.synthesis is not None:
        pairs.add((cfg.synthesis.provider, cfg.synthesis.model))
    if cfg.convergence == "judge" and cfg.judge is not None:
        pairs.add((cfg.judge.provider, cfg.judge.model))
    return pairs


def _toolsets_for_config(cfg: FleetConfig) -> set[str]:
    toolsets: set[str] = set()
    for s in cfg.specialists:
        toolsets.update(s.toolset)
    return toolsets


@contextlib.contextmanager
def matching_palette(*fleets: str | Path | FleetConfig) -> Iterator[Path]:
    """Patch ``CADRE_PALETTE`` to a temp palette.yaml covering every
    (provider, model) pair declared by ``fleets``, so
    ``cadre.preflight.preflight_refusal`` sees "all on palette" (returns
    ``None`` -- proceed) for every fleet passed, instead of refusing.

    Each item in ``fleets`` is a path/``Path`` to a fleet YAML (loaded via
    ``FleetConfig.load``) or an already-loaded ``FleetConfig``. Pass every
    fleet a test actually runs through ``run_command`` / the skill's
    ``main()`` -- a palette built from only SOME of them leaves the others
    off-palette-refused.

    Toolsets are unioned from every specialist declared across ``fleets``.
    ``preflight_refusal`` never checks toolsets (R4 -- models only), but the
    palette schema requires a well-formed ``toolsets:`` list; without one,
    ``load_palette`` collapses the whole file to ``None``, and
    ``preflight_refusal`` then treats a *present-but-unparseable* file as a
    malformed palette and refuses -- the opposite of this fixture's purpose.

    Yields the temp palette ``Path``. Restores the prior ``CADRE_PALETTE``
    value (or its absence) on exit and removes the temp file.
    """
    pairs: set[tuple[str, str]] = set()
    toolsets: set[str] = set()
    for fleet in fleets:
        cfg = fleet if isinstance(fleet, FleetConfig) else FleetConfig.load(fleet)
        pairs |= _pairs_for_config(cfg)
        toolsets |= _toolsets_for_config(cfg)

    fd, raw_path = tempfile.mkstemp(prefix="matching-palette-", suffix=".yaml")
    path = Path(raw_path)
    lines = ["generated_at: '2026-01-01T00:00:00.000000'"]
    # A bare "key:" with no items parses as YAML null, not an empty list --
    # load_palette's `isinstance(x, list)` check then collapses the WHOLE
    # palette to None (present-but-unparseable), which preflight_refusal
    # treats as a malformed palette and refuses -- the opposite of this
    # fixture's purpose. Use the explicit "[]" flow form whenever a fleet
    # (e.g. an all-`toolset: []` judge fleet) yields no entries.
    if pairs:
        lines.append("models:")
        for provider, model in sorted(pairs):
            lines.append(f"  - provider: {provider}")
            lines.append(f"    model: {model}")
    else:
        lines.append("models: []")
    if toolsets:
        lines.append("toolsets:")
        for t in sorted(toolsets):
            lines.append(f"  - {t}")
    else:
        lines.append("toolsets: []")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    try:
        with patch.dict(os.environ, {"CADRE_PALETTE": str(path)}):
            yield path
    finally:
        path.unlink(missing_ok=True)
