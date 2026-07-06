"""Tests for cadre/preflight.py — the #62 preflight-refuse gate.

Hermetic: palette files are written to tempfile.mkdtemp() (never ~/.cadre) and
injected via the ``palette_path`` param — the same convention test_preview_lint.py
uses for check_palette / render_preview_warnings. Runner-level wiring tests
(cadre.cli.run_command and cadre/data/skill/run.py) live in test_cli.py, since
they exercise the full runner, not just this module.

Coverage:
- preflight_refusal: off-palette specialist / synthesizer / judge → refusal;
  all on-palette → None; palette absent/malformed → None (degrade-open);
  off-palette toolset only → None (tight-scope guard, models only);
  control/bidi bytes in role/model → refusal renders inertly (sanitized);
  CADRE_PALETTE env resolution when no palette_path is given.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cadre.config import FleetConfig, JudgeSpec, SpecialistSpec, SynthesisSpec
from cadre.personas import resolve
from cadre.preflight import preflight_refusal
from cadre.preview_lint import Palette


# ---------------------------------------------------------------------------
# Fixtures (local copies — every test module in this repo defines its own,
# per the test_render.py / test_preview_lint.py precedent).
# ---------------------------------------------------------------------------


def _write_palette(tmp: Path, *, models=None, toolsets=None) -> Path:
    """Write a minimal valid palette YAML to ``tmp/palette.yaml`` and return the path."""
    if models is None:
        models = [
            {"provider": "xai", "model": "grok-4.3"},
            {"provider": "openrouter", "model": "google/gemini-3-flash"},
        ]
    if toolsets is None:
        toolsets = ["web", "search", "x_search"]
    lines = ["generated_at: '2026-07-05T00:00:00.000000'"]
    if models:
        lines.append("models:")
        for m in models:
            lines.append(f"  - provider: {m['provider']}")
            lines.append(f"    model: {m['model']}")
    else:
        lines.append("models: []")
    if toolsets:
        lines.append("toolsets:")
        for t in toolsets:
            lines.append(f"  - {t}")
    else:
        lines.append("toolsets: []")
    path = tmp / "palette.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_config(
    *,
    convergence="synthesize",
    specialist_role="web",
    specialist_provider="xai",
    specialist_model="grok-4.3",
    specialist_toolset=None,
    synth_provider="openrouter",
    synth_model="google/gemini-3-flash",
    judge_provider="openrouter",
    judge_model="anthropic/claude-opus-4.8",
) -> FleetConfig:
    """Build a minimal FleetConfig directly (no disk I/O)."""
    specialists = [
        SpecialistSpec(
            role=specialist_role,
            provider=specialist_provider,
            model=specialist_model,
            focus="cite a primary source with a link",
            toolset=specialist_toolset if specialist_toolset is not None else ["web"],
        )
    ]
    synthesis = None
    judge = None
    if convergence == "synthesize":
        synthesis = SynthesisSpec(provider=synth_provider, model=synth_model)
    elif convergence == "judge":
        judge = JudgeSpec(provider=judge_provider, model=judge_model)
    cfg = FleetConfig(
        name="test-fleet",
        specialists=specialists,
        synthesis=synthesis,
        judge=judge,
        convergence=convergence,
    )
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


class _PreflightTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)



# ---------------------------------------------------------------------------
# preflight_refusal — refusal cases
# ---------------------------------------------------------------------------


class TestPreflightRefusalOffPaletteSpecialist(_PreflightTestBase):
    def test_off_palette_specialist_refuses(self):
        path = _write_palette(
            self.tmp,
            models=[{"provider": "openrouter", "model": "google/gemini-3-flash"}],
            toolsets=["web"],
        )
        cfg = _make_config(specialist_provider="unknown", specialist_model="mystery/model")
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIsNotNone(refusal)

    def test_refusal_names_role_and_model(self):
        path = _write_palette(
            self.tmp,
            models=[{"provider": "openrouter", "model": "google/gemini-3-flash"}],
            toolsets=["web"],
        )
        cfg = _make_config(
            specialist_role="scout", specialist_provider="unknown", specialist_model="mystery/model"
        )
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIn("scout", refusal)
        self.assertIn("unknown", refusal)
        self.assertIn("mystery/model", refusal)

    def test_refusal_includes_fix_hint(self):
        path = _write_palette(self.tmp, models=[], toolsets=["web"])
        cfg = _make_config()
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIn("palette.yaml", refusal)
        self.assertIn("cadre verify-palette", refusal)

    def test_refusal_is_multiline(self):
        path = _write_palette(self.tmp, models=[], toolsets=["web"])
        cfg = _make_config()
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIn("\n", refusal)


class TestPreflightRefusalOffPaletteSynthesizer(_PreflightTestBase):
    def test_off_palette_synthesizer_refuses(self):
        path = _write_palette(
            self.tmp,
            models=[{"provider": "xai", "model": "grok-4.3"}],
            toolsets=["web"],
        )
        cfg = _make_config(
            convergence="synthesize",
            specialist_provider="xai", specialist_model="grok-4.3",
            synth_provider="unknown", synth_model="off-palette-synth",
        )
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIsNotNone(refusal)
        self.assertIn("synthesizer", refusal)
        self.assertIn("off-palette-synth", refusal)


class TestPreflightRefusalOffPaletteJudge(_PreflightTestBase):
    def test_off_palette_judge_refuses(self):
        path = _write_palette(
            self.tmp,
            models=[{"provider": "xai", "model": "grok-4.3"}],
            toolsets=["web"],
        )
        cfg = _make_config(
            convergence="judge",
            specialist_provider="xai", specialist_model="grok-4.3",
            judge_provider="unknown", judge_model="off-palette-judge",
        )
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIsNotNone(refusal)
        self.assertIn("judge", refusal)
        self.assertIn("off-palette-judge", refusal)


# ---------------------------------------------------------------------------
# preflight_refusal — proceed (None) cases
# ---------------------------------------------------------------------------


class TestPreflightRefusalAllOnPalette(_PreflightTestBase):
    def test_all_on_palette_proceeds(self):
        path = _write_palette(
            self.tmp,
            models=[
                {"provider": "xai", "model": "grok-4.3"},
                {"provider": "openrouter", "model": "google/gemini-3-flash"},
            ],
            toolsets=["web"],
        )
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        self.assertIsNone(preflight_refusal(cfg, palette_path=path))

    def test_all_on_palette_judge_mode_proceeds(self):
        path = _write_palette(
            self.tmp,
            models=[
                {"provider": "xai", "model": "grok-4.3"},
                {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            ],
            toolsets=["web"],
        )
        cfg = _make_config(
            convergence="judge",
            specialist_provider="xai", specialist_model="grok-4.3",
            judge_provider="openrouter", judge_model="anthropic/claude-opus-4.8",
        )
        self.assertIsNone(preflight_refusal(cfg, palette_path=path))

    def test_all_on_palette_collect_mode_proceeds(self):
        """Collect fleets have no synthesizer/judge; only specialists are checked."""
        path = _write_palette(
            self.tmp,
            models=[{"provider": "xai", "model": "grok-4.3"}],
            toolsets=["web"],
        )
        cfg = _make_config(convergence="collect", specialist_provider="xai", specialist_model="grok-4.3")
        self.assertIsNone(preflight_refusal(cfg, palette_path=path))


class TestPreflightRefusalNoPalette(_PreflightTestBase):
    """Palette absent or malformed → degrade-open (proceed), matching the preview posture."""

    def test_absent_palette_proceeds(self):
        missing = self.tmp / "no_palette.yaml"
        cfg = _make_config(specialist_provider="unknown", specialist_model="mystery")
        self.assertIsNone(preflight_refusal(cfg, palette_path=missing))

    def test_malformed_palette_proceeds(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("not: valid: yaml: [\n", encoding="utf-8")
        cfg = _make_config(specialist_provider="unknown", specialist_model="mystery")
        self.assertIsNone(preflight_refusal(cfg, palette_path=bad))

    def test_structurally_invalid_palette_proceeds(self):
        """A palette missing the required models/toolsets keys → load_palette
        returns None → preflight degrades open, same as a missing file."""
        bad = self.tmp / "no_keys.yaml"
        bad.write_text("generated_at: 'x'\n", encoding="utf-8")
        cfg = _make_config(specialist_provider="unknown", specialist_model="mystery")
        self.assertIsNone(preflight_refusal(cfg, palette_path=bad))

    def test_cadre_palette_env_used_when_no_param(self):
        """CADRE_PALETTE env is honored when palette_path is not given (matches
        load_palette's own resolution order)."""
        path = _write_palette(
            self.tmp,
            models=[{"provider": "xai", "model": "grok-4.3"}],
            toolsets=["web"],
        )
        cfg = _make_config(specialist_provider="unknown", specialist_model="mystery")
        with patch.dict(os.environ, {"CADRE_PALETTE": str(path)}):
            refusal = preflight_refusal(cfg)
        self.assertIsNotNone(refusal)

    def test_no_env_no_param_default_path_absent_proceeds(self):
        """No CADRE_PALETTE, no param → default path; deterministic via a patched
        DEFAULT_PALETTE_PATH pointed at a guaranteed-missing file (mirrors
        test_preview_lint.py's equivalent load_palette test)."""
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_PALETTE"}
        missing = str(self.tmp / "definitely-missing-palette.yaml")
        cfg = _make_config(specialist_provider="unknown", specialist_model="mystery")
        with patch.dict(os.environ, env_without, clear=True), \
                patch("cadre.preview_lint.DEFAULT_PALETTE_PATH", missing):
            refusal = preflight_refusal(cfg)
        self.assertIsNone(refusal)


class TestPreflightRefusalToolsetOnlyTightScope(_PreflightTestBase):
    """R4 tight-scope guard: an off-palette TOOLSET alone never refuses — models only."""

    def test_off_palette_toolset_models_on_palette_proceeds(self):
        path = _write_palette(
            self.tmp,
            models=[
                {"provider": "xai", "model": "grok-4.3"},
                {"provider": "openrouter", "model": "google/gemini-3-flash"},
            ],
            toolsets=["web"],  # "vision" deliberately absent
        )
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["vision"],  # off-palette toolset, on-palette model
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        # Sanity: check_palette still WARNS on the toolset (proves this is a real
        # off-palette condition, not a fixture mistake) — but preflight must not refuse.
        from cadre.preview_lint import check_palette, load_palette

        palette = load_palette(path)
        toolset_warnings = [w for w in check_palette(cfg, palette) if "toolset" in w]
        self.assertTrue(toolset_warnings, "fixture should produce a toolset warning")

        self.assertIsNone(preflight_refusal(cfg, palette_path=path))


# ---------------------------------------------------------------------------
# preflight_refusal — trust-surface sanitization
# ---------------------------------------------------------------------------


class TestPreflightRefusalSanitized(_PreflightTestBase):
    """A refusal is a control-surface print — a tampered fleet must not inject
    terminal escapes into it (mirrors the check_palette escape-safety tests)."""

    def test_escape_in_model_stripped_from_refusal(self):
        path = _write_palette(self.tmp, models=[], toolsets=["web"])
        cfg = _make_config()
        cfg.specialists[0].model = "m\x1b[2Kevil"
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIsNotNone(refusal)
        self.assertNotIn("\x1b", refusal)

    def test_escape_in_role_stripped_from_refusal(self):
        path = _write_palette(self.tmp, models=[], toolsets=["web"])
        cfg = _make_config()
        cfg.specialists[0].role = "scan\x1b[2J"
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIsNotNone(refusal)
        self.assertNotIn("\x1b", refusal)

    def test_bidi_bytes_in_provider_stripped_from_refusal(self):
        path = _write_palette(self.tmp, models=[], toolsets=["web"])
        cfg = _make_config()
        cfg.specialists[0].provider = "xai‮-evil"  # RLO bidi override
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIsNotNone(refusal)
        self.assertNotIn("‮", refusal)

    def test_newline_in_model_cannot_forge_a_line(self):
        path = _write_palette(self.tmp, models=[], toolsets=["web"])
        cfg = _make_config()
        cfg.specialists[0].model = "m\nRefused: fake line\x1b[2K"
        refusal = preflight_refusal(cfg, palette_path=path)
        self.assertIsNotNone(refusal)
        # Single-line sanitize (multiline=False by default in _sanitize) drops
        # the embedded newline entirely — no standalone forged line appears.
        forged = [ln for ln in refusal.split("\n") if ln.strip() == "Refused: fake line"]
        self.assertEqual(forged, [])


if __name__ == "__main__":
    unittest.main()
