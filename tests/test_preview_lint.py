"""Tests for fleet_engine/preview_lint.py — palette validation (U5) and focus lint (U6).

All tests are hermetic: palette files are written to tempfile.mkdtemp() (never
~/.cadre), and CADRE_PALETTE is patched via patch.dict so tests don't leak env
state. This follows the test_palette.py / test_config.py precedent of "never
touch ~/.cadre."

Coverage:
- load_palette: missing file, malformed YAML, non-mapping root, missing/non-list
  keys, malformed entries, valid round-trip, CADRE_PALETTE env override.
- check_palette: off-palette model warning, off-palette toolset warning, all
  on-palette → empty list, synthesizer check (synthesize vs. collect convergence).
- check_focus_grounding: retrieval lane with bare focus → warn; anti-grounding
  focus → warn (anti-grounding copy); "After" focus (grounding-control doc) → no
  warn (critical non-false-positive); per-claim sourcing focus → no warn;
  non-retrieval lane → no warn; multi-lane partial grounding → exactly one warn.
- render_preview_warnings: missing palette → skipped note; valid palette with
  warnings → ⚠ block; all clean → ✓ line; warn-never-block (no exception);
  palette=None + focus-lint-failing fleet → ⚠ block + skipped note (U6 path).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fleet_engine.config import FleetConfig, SpecialistSpec, SynthesisSpec
from fleet_engine.personas import resolve
from fleet_engine.preview_lint import (
    DEFAULT_PALETTE_PATH,
    RETRIEVAL_TOOLSETS,
    Palette,
    check_focus_grounding,
    check_palette,
    load_palette,
    render_preview_warnings,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_palette(tmp: Path, *, models=None, toolsets=None, extra="") -> Path:
    """Write a minimal valid palette YAML to ``tmp/palette.yaml`` and return the path.

    Empty lists are written as ``[]`` to avoid YAML parsing them as None.
    """
    if models is None:
        models = [
            {"provider": "xai", "model": "grok-4.3"},
            {"provider": "openrouter", "model": "google/gemini-3-flash"},
        ]
    if toolsets is None:
        toolsets = ["web", "search", "x_search", "vision"]
    lines = ["generated_at: '2026-06-18T14:30:00.000000'"]
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
    if extra:
        lines.append(extra)
    path = tmp / "palette.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_config(
    *,
    convergence="synthesize",
    specialist_provider="xai",
    specialist_model="grok-4.3",
    specialist_toolset=None,
    specialist_focus="",
    synth_provider="openrouter",
    synth_model="google/gemini-3-flash",
) -> FleetConfig:
    """Build a minimal FleetConfig directly without loading from disk."""
    specialists = [
        SpecialistSpec(
            role="web",
            provider=specialist_provider,
            model=specialist_model,
            focus=specialist_focus,
            toolset=specialist_toolset if specialist_toolset is not None else ["web"],
        )
    ]
    synthesis = None
    if convergence == "synthesize":
        synthesis = SynthesisSpec(
            provider=synth_provider,
            model=synth_model,
        )
    cfg = FleetConfig(
        name="test-fleet",
        specialists=specialists,
        synthesis=synthesis,
        convergence=convergence,
    )
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


# ---------------------------------------------------------------------------
# Tests: load_palette
# ---------------------------------------------------------------------------


class TestLoadPaletteMissingFile(unittest.TestCase):
    """load_palette returns None when no file exists at the path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_missing_file_returns_none(self):
        path = self.tmp / "does_not_exist.yaml"
        result = load_palette(path)
        self.assertIsNone(result)

    def test_missing_file_does_not_raise(self):
        path = self.tmp / "no_such.yaml"
        try:
            load_palette(path)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"load_palette raised on missing file: {exc}")


class TestLoadPaletteMalformedYAML(unittest.TestCase):
    """load_palette returns None for YAML parse errors and bad encodings."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_invalid_yaml_returns_none(self):
        p = self.tmp / "bad.yaml"
        p.write_text("name: [unterminated\n", encoding="utf-8")
        self.assertIsNone(load_palette(p))

    def test_non_mapping_root_returns_none(self):
        p = self.tmp / "list.yaml"
        p.write_text("- a\n- b\n", encoding="utf-8")
        self.assertIsNone(load_palette(p))

    def test_scalar_root_returns_none(self):
        p = self.tmp / "scalar.yaml"
        p.write_text("just a string\n", encoding="utf-8")
        self.assertIsNone(load_palette(p))

    def test_empty_file_returns_none(self):
        # yaml.safe_load of empty → None (not a dict) → None
        p = self.tmp / "empty.yaml"
        p.write_text("", encoding="utf-8")
        self.assertIsNone(load_palette(p))


class TestLoadPaletteStructurallyInvalid(unittest.TestCase):
    """load_palette returns None for structurally invalid palettes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_missing_models_key_returns_none(self):
        p = self.tmp / "no_models.yaml"
        p.write_text("toolsets: [web]\n", encoding="utf-8")
        self.assertIsNone(load_palette(p))

    def test_models_not_list_returns_none(self):
        p = self.tmp / "models_scalar.yaml"
        p.write_text("models: oops\ntoolsets: [web]\n", encoding="utf-8")
        self.assertIsNone(load_palette(p))

    def test_missing_toolsets_key_returns_none(self):
        p = self.tmp / "no_toolsets.yaml"
        p.write_text("models:\n  - provider: xai\n    model: grok-4.3\n", encoding="utf-8")
        self.assertIsNone(load_palette(p))

    def test_toolsets_not_list_returns_none(self):
        p = self.tmp / "toolsets_scalar.yaml"
        p.write_text(
            "models:\n  - provider: xai\n    model: grok-4.3\ntoolsets: web\n",
            encoding="utf-8",
        )
        self.assertIsNone(load_palette(p))

    def test_model_entry_missing_provider_returns_none(self):
        p = self.tmp / "no_provider.yaml"
        p.write_text(
            "models:\n  - model: grok-4.3\ntoolsets: [web]\n",
            encoding="utf-8",
        )
        self.assertIsNone(load_palette(p))

    def test_model_entry_missing_model_returns_none(self):
        p = self.tmp / "no_model.yaml"
        p.write_text(
            "models:\n  - provider: xai\ntoolsets: [web]\n",
            encoding="utf-8",
        )
        self.assertIsNone(load_palette(p))

    def test_model_entry_not_dict_returns_none(self):
        p = self.tmp / "entry_scalar.yaml"
        p.write_text(
            "models:\n  - just-a-string\ntoolsets: [web]\n",
            encoding="utf-8",
        )
        self.assertIsNone(load_palette(p))


class TestLoadPaletteValid(unittest.TestCase):
    """load_palette correctly parses a valid palette."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_returns_palette_with_models(self):
        path = _write_palette(
            self.tmp,
            models=[{"provider": "xai", "model": "grok-4.3"}],
            toolsets=["web"],
        )
        result = load_palette(path)
        self.assertIsNotNone(result)
        self.assertIn(("xai", "grok-4.3"), result.models)

    def test_returns_palette_with_toolsets(self):
        path = _write_palette(
            self.tmp,
            models=[{"provider": "xai", "model": "grok-4.3"}],
            toolsets=["web", "x_search"],
        )
        result = load_palette(path)
        self.assertIsNotNone(result)
        self.assertIn("web", result.toolsets)
        self.assertIn("x_search", result.toolsets)

    def test_multiple_models_all_present(self):
        path = _write_palette(self.tmp)
        result = load_palette(path)
        self.assertIsNotNone(result)
        self.assertIn(("xai", "grok-4.3"), result.models)
        self.assertIn(("openrouter", "google/gemini-3-flash"), result.models)

    def test_models_is_a_set_not_list(self):
        path = _write_palette(self.tmp)
        result = load_palette(path)
        self.assertIsInstance(result.models, set)

    def test_toolsets_is_a_set_not_list(self):
        path = _write_palette(self.tmp)
        result = load_palette(path)
        self.assertIsInstance(result.toolsets, set)


class TestLoadPalettePathResolution(unittest.TestCase):
    """load_palette resolves path via param → CADRE_PALETTE env → DEFAULT_PALETTE_PATH."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_explicit_path_param_used(self):
        path = _write_palette(self.tmp)
        result = load_palette(path)
        self.assertIsNotNone(result)

    def test_cadre_palette_env_override(self):
        path = _write_palette(self.tmp)
        # Clear any explicit param — env should be used instead of DEFAULT.
        with patch.dict(os.environ, {"CADRE_PALETTE": str(path)}):
            result = load_palette(None)  # no explicit param
        self.assertIsNotNone(result)
        self.assertIn(("xai", "grok-4.3"), result.models)

    def test_cadre_palette_env_missing_file_returns_none(self):
        bad = str(self.tmp / "no_palette.yaml")
        with patch.dict(os.environ, {"CADRE_PALETTE": bad}):
            result = load_palette(None)
        self.assertIsNone(result)

    def test_no_env_no_param_uses_default_path(self):
        """No CADRE_PALETTE and no param → resolves the default path; returns None when absent.

        Deterministic: patch DEFAULT_PALETTE_PATH to a guaranteed-missing temp path so the
        assertion holds regardless of whether the dev box has a real ~/.cadre/palette.yaml.
        """
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_PALETTE"}
        missing = str(self.tmp / "definitely-missing-palette.yaml")
        with patch.dict(os.environ, env_without, clear=True), \
                patch("fleet_engine.preview_lint.DEFAULT_PALETTE_PATH", missing):
            result = load_palette(None)
        self.assertIsNone(result)

    def test_explicit_param_takes_priority_over_env(self):
        """Explicit path param beats CADRE_PALETTE env."""
        env_path = _write_palette(self.tmp, models=[{"provider": "env", "model": "env-model"}], toolsets=["web"])
        param_path = _write_palette(
            Path(tempfile.mkdtemp()),
            models=[{"provider": "param", "model": "param-model"}],
            toolsets=["web"],
        )
        with patch.dict(os.environ, {"CADRE_PALETTE": str(env_path)}):
            result = load_palette(param_path)
        self.assertIsNotNone(result)
        self.assertIn(("param", "param-model"), result.models)
        self.assertNotIn(("env", "env-model"), result.models)


# ---------------------------------------------------------------------------
# Tests: check_palette (pure, no I/O)
# ---------------------------------------------------------------------------


class TestCheckPaletteModels(unittest.TestCase):
    """check_palette warns on off-palette specialist (provider, model) pairs."""

    def setUp(self):
        self.palette = Palette(
            models={("xai", "grok-4.3"), ("openrouter", "google/gemini-3-flash")},
            toolsets={"web", "search", "x_search"},
        )

    def test_on_palette_specialist_no_warning(self):
        cfg = _make_config(specialist_provider="xai", specialist_model="grok-4.3",
                           synth_provider="openrouter", synth_model="google/gemini-3-flash")
        warnings = check_palette(cfg, self.palette)
        # No model warnings (toolset "web" is on palette too)
        model_warnings = [w for w in warnings if "not in palette" in w]
        self.assertEqual(model_warnings, [])

    def test_off_palette_specialist_warns(self):
        cfg = _make_config(
            specialist_provider="unknown", specialist_model="mystery/model",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, self.palette)
        self.assertTrue(any("unknown" in w and "mystery/model" in w for w in warnings),
                        f"expected off-palette model warning, got: {warnings}")

    def test_off_palette_specialist_warning_names_role(self):
        cfg = _make_config(
            specialist_provider="unknown", specialist_model="mystery/model",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, self.palette)
        self.assertTrue(any("web" in w for w in warnings),  # role is "web" in _make_config
                        "warning should name the specialist role")

    def test_off_palette_specialist_warning_includes_hint(self):
        cfg = _make_config(
            specialist_provider="unknown", specialist_model="mystery/model",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, self.palette)
        self.assertTrue(any("palette.yaml" in w for w in warnings),
                        "warning should include a swap hint")


class TestCheckPaletteToolsets(unittest.TestCase):
    """check_palette warns on off-palette specialist toolset entries."""

    def setUp(self):
        self.palette = Palette(
            models={("xai", "grok-4.3"), ("openrouter", "google/gemini-3-flash")},
            toolsets={"web", "search", "x_search"},
        )

    def test_on_palette_toolset_no_warning(self):
        cfg = _make_config(
            specialist_toolset=["web"],
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, self.palette)
        toolset_warnings = [w for w in warnings if "toolset" in w]
        self.assertEqual(toolset_warnings, [])

    def test_off_palette_toolset_warns(self):
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["vision"],  # not in palette
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, self.palette)
        self.assertTrue(any("vision" in w and "toolset" in w for w in warnings),
                        f"expected toolset warning for 'vision', got: {warnings}")

    def test_off_palette_toolset_warning_names_role(self):
        cfg = _make_config(
            specialist_toolset=["vision"],
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, self.palette)
        self.assertTrue(any("web" in w for w in warnings),
                        "warning should name the specialist role")

    def test_empty_toolset_no_warning(self):
        """A specialist with no toolset generates no toolset warning."""
        cfg = _make_config(
            specialist_toolset=[],
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, self.palette)
        toolset_warnings = [w for w in warnings if "toolset" in w]
        self.assertEqual(toolset_warnings, [])


class TestCheckPaletteSynthesizer(unittest.TestCase):
    """check_palette synthesizer model check is convergence-aware."""

    def setUp(self):
        self.palette = Palette(
            models={("xai", "grok-4.3"), ("openrouter", "google/gemini-3-flash")},
            toolsets={"web", "x_search"},
        )

    def test_synthesize_off_palette_synth_warns(self):
        """Synthesize fleet with off-palette synthesizer → warned."""
        cfg = _make_config(
            convergence="synthesize",
            specialist_provider="xai", specialist_model="grok-4.3",
            synth_provider="unknown", synth_model="off-palette",
        )
        warnings = check_palette(cfg, self.palette)
        self.assertTrue(any("synthesizer" in w for w in warnings),
                        f"expected synthesizer warning, got: {warnings}")

    def test_synthesize_on_palette_synth_no_warning(self):
        """Synthesize fleet with on-palette synthesizer → no synthesizer warning."""
        cfg = _make_config(
            convergence="synthesize",
            specialist_provider="xai", specialist_model="grok-4.3",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, self.palette)
        synth_warnings = [w for w in warnings if "synthesizer" in w]
        self.assertEqual(synth_warnings, [])

    def test_collect_fleet_synthesis_none_no_crash(self):
        """Collect fleet with synthesis=None → no AttributeError, no synthesizer warning."""
        cfg = _make_config(
            convergence="collect",
            specialist_provider="xai", specialist_model="grok-4.3",
        )
        self.assertIsNone(cfg.synthesis, "collect fleet must have synthesis=None")
        try:
            warnings = check_palette(cfg, self.palette)
        except AttributeError as exc:
            self.fail(f"check_palette raised AttributeError on collect fleet: {exc}")
        synth_warnings = [w for w in warnings if "synthesizer" in w]
        self.assertEqual(synth_warnings, [])

    def test_collect_fleet_only_specialists_checked(self):
        """Collect fleet: off-palette specialist is warned; synthesizer is never inspected."""
        cfg = _make_config(
            convergence="collect",
            specialist_provider="unknown", specialist_model="off/model",
        )
        warnings = check_palette(cfg, self.palette)
        # Specialist warned, synthesizer not mentioned (synthesis is None)
        self.assertTrue(any("unknown" in w for w in warnings),
                        "off-palette specialist should warn")
        self.assertFalse(any("synthesizer" in w for w in warnings),
                         "synthesizer must not be checked in collect mode")


class TestCheckPaletteAllOnPalette(unittest.TestCase):
    """check_palette returns empty list when everything is on-palette."""

    def test_all_on_palette_returns_empty(self):
        palette = Palette(
            models={("xai", "grok-4.3"), ("openrouter", "google/gemini-3-flash")},
            toolsets={"web", "x_search"},
        )
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["web"],
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        warnings = check_palette(cfg, palette)
        self.assertEqual(warnings, [])


# ---------------------------------------------------------------------------
# Tests: render_preview_warnings
# ---------------------------------------------------------------------------


class TestRenderPreviewWarningsMissingPalette(unittest.TestCase):
    """render_preview_warnings notes "validation skipped" when no palette is present."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_missing_palette_returns_skipped_note(self):
        cfg = _make_config()
        missing = self.tmp / "no_palette.yaml"
        result = render_preview_warnings(cfg, palette_path=missing)
        self.assertIn("validation skipped", result.lower())

    def test_missing_palette_no_exception(self):
        cfg = _make_config()
        missing = self.tmp / "no_palette.yaml"
        try:
            render_preview_warnings(cfg, palette_path=missing)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"render_preview_warnings raised on missing palette: {exc}")

    def test_missing_palette_result_is_string(self):
        cfg = _make_config()
        missing = self.tmp / "no_palette.yaml"
        result = render_preview_warnings(cfg, palette_path=missing)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_missing_palette_skipped_note_mentions_path(self):
        cfg = _make_config()
        missing = self.tmp / "no_palette.yaml"
        result = render_preview_warnings(cfg, palette_path=missing)
        # The resolved path should appear in the note.
        self.assertIn(str(missing), result)


class TestRenderPreviewWarningsWithWarnings(unittest.TestCase):
    """render_preview_warnings formats a ⚠ block when there are warnings."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_off_palette_model_appears_in_output(self):
        palette_path = _write_palette(
            self.tmp,
            models=[{"provider": "openrouter", "model": "google/gemini-3-flash"}],
            toolsets=["web"],
        )
        cfg = _make_config(
            specialist_provider="unknown", specialist_model="mystery/model",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        result = render_preview_warnings(cfg, palette_path=palette_path)
        self.assertIn("unknown", result)
        self.assertIn("mystery/model", result)

    def test_warnings_block_has_warning_header(self):
        palette_path = _write_palette(
            self.tmp,
            models=[{"provider": "openrouter", "model": "google/gemini-3-flash"}],
            toolsets=["web"],
        )
        cfg = _make_config(
            specialist_provider="unknown", specialist_model="mystery/model",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        result = render_preview_warnings(cfg, palette_path=palette_path)
        self.assertIn("⚠", result)
        self.assertIn("fleet validation", result)

    def test_warning_count_in_header(self):
        palette_path = _write_palette(
            self.tmp,
            models=[],
            toolsets=[],
        )
        # Empty palette → specialist model off, synth model off, toolset off
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["web"],
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        result = render_preview_warnings(cfg, palette_path=palette_path)
        self.assertIn("warning", result)
        # Should say N warning(s) where N > 0
        self.assertRegex(result, r"\d+ warning")

    def test_no_exception_off_palette(self):
        palette_path = _write_palette(self.tmp, models=[], toolsets=[])
        cfg = _make_config()
        try:
            render_preview_warnings(cfg, palette_path=palette_path)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"raised unexpectedly: {exc}")


class TestRenderPreviewWarningsAllOnPalette(unittest.TestCase):
    """render_preview_warnings returns ✓ line when all models/toolsets are on-palette."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_all_on_palette_returns_check_mark_line(self):
        palette_path = _write_palette(
            self.tmp,
            models=[
                {"provider": "xai", "model": "grok-4.3"},
                {"provider": "openrouter", "model": "google/gemini-3-flash"},
            ],
            toolsets=["web"],
        )
        # Focus must include a sourcing directive so focus-lint is also clean.
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["web"],
            specialist_focus="Cite a primary source with a link for every claim.",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        result = render_preview_warnings(cfg, palette_path=palette_path)
        self.assertIn("✓", result)
        self.assertIn("fleet validation", result)
        self.assertNotIn("⚠", result)


class TestRenderPreviewWarningsWarnNeverBlock(unittest.TestCase):
    """render_preview_warnings never raises regardless of palette state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_no_exception_for_missing_palette(self):
        cfg = _make_config()
        try:
            render_preview_warnings(cfg, palette_path=self.tmp / "absent.yaml")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"raised: {exc}")

    def test_no_exception_for_malformed_palette(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("not: valid: yaml: [\n", encoding="utf-8")
        cfg = _make_config()
        try:
            render_preview_warnings(cfg, palette_path=bad)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"raised: {exc}")

    def test_no_exception_for_collect_fleet_synthesis_none(self):
        """Collect fleet (synthesis=None) must not crash render_preview_warnings."""
        palette_path = _write_palette(self.tmp)
        cfg = _make_config(convergence="collect",
                           specialist_provider="xai", specialist_model="grok-4.3",
                           specialist_toolset=["web"])
        try:
            render_preview_warnings(cfg, palette_path=palette_path)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"raised on collect fleet: {exc}")

    def test_cadre_palette_env_used_when_no_param(self):
        """CADRE_PALETTE env is used as the fallback when no param is given."""
        palette_path = _write_palette(
            self.tmp,
            models=[{"provider": "xai", "model": "grok-4.3"},
                    {"provider": "openrouter", "model": "google/gemini-3-flash"}],
            toolsets=["web"],
        )
        # Focus must include a sourcing directive so focus-lint is also clean.
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["web"],
            specialist_focus="Cite a primary source with a link for every claim.",
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        with patch.dict(os.environ, {"CADRE_PALETTE": str(palette_path)}):
            result = render_preview_warnings(cfg)  # no palette_path param
        self.assertIn("✓", result)


# ---------------------------------------------------------------------------
# Tests: check_focus_grounding — U6
#
# Fixtures from docs/solutions/design-patterns/specialist-focus-grounding-control.md:
#   BEFORE focus: "Fast, broad coverage — enumerate the full landscape of options
#     so nothing obvious is missed. Breadth over depth."
#   AFTER focus: BEFORE + " Cite a real, current primary source (with a link)
#     for every item; if you can't find one, mark the item as unsourced rather
#     than asserting it."
# The AFTER example is the critical non-false-positive: it has anti-grounding
# phrasing AND a sourcing directive → must NOT warn.
# ---------------------------------------------------------------------------

_BEFORE_FOCUS = (
    "Fast, broad coverage — enumerate the full landscape of options so nothing "
    "obvious is missed. Breadth over depth."
)
_AFTER_FOCUS = (
    "Fast, broad coverage — enumerate the full landscape of options so nothing "
    "obvious is missed. Breadth over depth. Cite a real, current primary source "
    "(with a link) for every item; if you can't find one, mark the item as "
    "unsourced rather than asserting it."
)


def _make_focus_config(
    *,
    role="scan",
    toolset=None,
    focus="",
    convergence="synthesize",
) -> FleetConfig:
    """Build a FleetConfig with a single specialist for focus-lint tests."""
    if toolset is None:
        toolset = ["web"]
    specialists = [
        SpecialistSpec(role=role, provider="xai", model="grok-4.3",
                       focus=focus, toolset=toolset)
    ]
    synthesis = None
    if convergence == "synthesize":
        synthesis = SynthesisSpec(provider="openrouter", model="google/gemini-3-flash")
    cfg = FleetConfig(
        name="test-fleet",
        specialists=specialists,
        synthesis=synthesis,
        convergence=convergence,
    )
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


class TestCheckFocusGroundingRetrievalWarn(unittest.TestCase):
    """check_focus_grounding warns for retrieval lanes with no sourcing directive."""

    def test_bare_topical_focus_warns(self):
        """Retrieval lane with a bare topical focus and no sourcing language → warn."""
        cfg = _make_focus_config(
            focus="Deep structured analysis and extraction; cross-check the other lanes.",
            toolset=["web"],
        )
        warnings = check_focus_grounding(cfg)
        self.assertEqual(len(warnings), 1)
        self.assertIn("scan", warnings[0])  # role named

    def test_bare_topical_focus_warning_mentions_sourcing_fix(self):
        """Bare-focus warning tells the operator to add a sourcing directive."""
        cfg = _make_focus_config(
            focus="Deep structured analysis and extraction; cross-check the other lanes.",
            toolset=["web"],
        )
        warnings = check_focus_grounding(cfg)
        self.assertTrue(any("cite" in w.lower() for w in warnings),
                        f"warning should mention cite directive, got: {warnings}")

    def test_incidental_source_substring_still_warns(self):
        """A retrieval focus whose only 'source'-ish word is 'resources' (no citation
        demand) still warns — word-boundary matching excludes the incidental substring."""
        cfg = _make_focus_config(
            focus="Review the available resources for the topic.",
            toolset=["web"],
        )
        warnings = check_focus_grounding(cfg)
        self.assertEqual(len(warnings), 1)

    def test_before_focus_anti_grounding_warns(self):
        """Grounding-control 'Before' focus (anti-grounding, no sourcing) → warn."""
        cfg = _make_focus_config(focus=_BEFORE_FOCUS, toolset=["web"])
        warnings = check_focus_grounding(cfg)
        self.assertEqual(len(warnings), 1)

    def test_before_focus_uses_anti_grounding_copy(self):
        """Anti-grounding phrasing triggers the more specific warning message."""
        cfg = _make_focus_config(focus=_BEFORE_FOCUS, toolset=["web"])
        warnings = check_focus_grounding(cfg)
        # The anti-grounding branch message contains breadth/speed framing mention.
        self.assertTrue(any("anti-grounding" in w for w in warnings),
                        f"expected anti-grounding copy, got: {warnings}")

    def test_empty_focus_warns(self):
        """An empty focus on a retrieval lane → warn (no sourcing directive)."""
        cfg = _make_focus_config(focus="", toolset=["web"])
        warnings = check_focus_grounding(cfg)
        self.assertEqual(len(warnings), 1)

    def test_warning_includes_profile_caveat(self):
        """Every focus warning includes the profile-scoped-tool caveat."""
        cfg = _make_focus_config(focus=_BEFORE_FOCUS, toolset=["web"])
        warnings = check_focus_grounding(cfg)
        self.assertTrue(any("profile" in w.lower() for w in warnings),
                        f"warning should include profile caveat, got: {warnings}")

    def test_warning_includes_runbook_reference(self):
        """Every focus warning points to docs/RUNBOOK.md."""
        cfg = _make_focus_config(focus=_BEFORE_FOCUS, toolset=["web"])
        warnings = check_focus_grounding(cfg)
        self.assertTrue(any("RUNBOOK" in w for w in warnings),
                        f"warning should mention RUNBOOK.md, got: {warnings}")


class TestCheckFocusGroundingRetrievalNoWarn(unittest.TestCase):
    """check_focus_grounding is silent for grounded retrieval lanes."""

    def test_after_focus_no_warn(self):
        """Grounding-control 'After' focus has sourcing despite anti-grounding → NO warn.

        This is the critical non-false-positive: 'breadth over depth' is present
        but 'cite a real, current primary source (with a link)' overrides it.
        """
        cfg = _make_focus_config(focus=_AFTER_FOCUS, toolset=["web"])
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [],
                         f"'After' focus must not warn, got: {warnings}")

    def test_per_claim_sources_no_warn(self):
        """A focus demanding per-claim sources/links with an unsourced fallback → no warn."""
        cfg = _make_focus_config(
            focus=(
                "Enumerate all relevant frameworks. Cite a primary source with a link "
                "for every item; mark as unsourced if none is found."
            ),
            toolset=["web"],
        )
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [])

    def test_focus_with_url_term_no_warn(self):
        """Focus containing 'url' counts as a sourcing directive → no warn."""
        cfg = _make_focus_config(
            focus="Return each finding with a url to the source document.",
            toolset=["web"],
        )
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [])

    def test_focus_with_reference_term_no_warn(self):
        """Focus containing 'reference' counts as a sourcing directive → no warn."""
        cfg = _make_focus_config(
            focus="Primary sources, papers, docs — include references for each claim.",
            toolset=["x_search"],
        )
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [])

    def test_focus_with_attribution_term_no_warn(self):
        """Focus containing 'attribution' (matched by 'attribut') → no warn."""
        cfg = _make_focus_config(
            focus="Real-time X / social — full attribution for each post or thread.",
            toolset=["x_search"],
        )
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [])

    def test_unsourced_hedge_without_demand_warns(self):
        """The 'unsourced' hedge ALONE (no citation demand) is not grounding — it warns.

        Per the grounding-control learning, grounding needs the DEMAND ("cite a real
        source / link") AND the hedge ("mark unsourced"); the hedge by itself gives the
        lane no instruction to seek sources. Word-boundary matching means "unsourced"
        no longer satisfies the "source" stem, so a hedge-only focus correctly warns.
        A realistic grounded focus still passes via its demand words (cite/source/link).
        """
        cfg = _make_focus_config(
            focus="Cover the landscape. Mark anything you can't verify as unsourced.",
            toolset=["web"],
        )
        warnings = check_focus_grounding(cfg)
        self.assertEqual(len(warnings), 1)


class TestCheckFocusGroundingNonRetrieval(unittest.TestCase):
    """check_focus_grounding does not check non-retrieval lanes."""

    def test_empty_toolset_no_warn(self):
        """A specialist with no toolset is not a retrieval lane → no warn."""
        cfg = _make_focus_config(focus="Deep structured analysis.", toolset=[])
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [])

    def test_non_retrieval_toolset_no_warn(self):
        """A lane with only 'vision' toolset is not retrieval → no warn."""
        cfg = _make_focus_config(focus="Describe what you see.", toolset=["vision"])
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [])

    def test_synthesize_convergence_no_warn_on_synthesis(self):
        """check_focus_grounding only checks specialists, not the synthesizer."""
        # Synthesize fleet: specialist is non-retrieval; synthesizer has no toolset.
        cfg = _make_focus_config(
            focus="Bare topical focus with no sourcing.",
            toolset=["vision"],
            convergence="synthesize",
        )
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [])


class TestCheckFocusGroundingMultiLane(unittest.TestCase):
    """check_focus_grounding produces exactly one warning per ungrounded retrieval lane."""

    def test_one_grounded_one_ungrounded_returns_exactly_one_warning(self):
        """Multi-lane fleet: grounded lane + ungrounded lane → exactly one warning."""
        specialists = [
            SpecialistSpec(
                role="scan",
                provider="xai", model="grok-4.3",
                focus=_BEFORE_FOCUS,  # ungrounded: anti-grounding, no sourcing
                toolset=["web"],
            ),
            SpecialistSpec(
                role="depth",
                provider="openrouter", model="google/gemini-3-flash",
                focus=_AFTER_FOCUS,  # grounded: has citation directive
                toolset=["web"],
            ),
        ]
        synthesis = SynthesisSpec(provider="openrouter", model="google/gemini-3-flash")
        cfg = FleetConfig(
            name="multi-lane",
            specialists=specialists,
            synthesis=synthesis,
            convergence="synthesize",
        )
        resolve(cfg, "/unused")
        warnings = check_focus_grounding(cfg)
        self.assertEqual(len(warnings), 1)
        self.assertIn("scan", warnings[0])
        self.assertNotIn("depth", warnings[0])

    def test_all_retrieval_ungrounded_returns_one_per_lane(self):
        """All retrieval lanes ungrounded → one warning per lane."""
        specialists = [
            SpecialistSpec(role="lane1", provider="xai", model="grok-4.3",
                           focus="Scan broadly.", toolset=["web"]),
            SpecialistSpec(role="lane2", provider="xai", model="grok-4.3",
                           focus="Scan socials.", toolset=["x_search"]),
        ]
        synthesis = SynthesisSpec(provider="openrouter", model="google/gemini-3-flash")
        cfg = FleetConfig(name="multi", specialists=specialists,
                          synthesis=synthesis, convergence="synthesize")
        resolve(cfg, "/unused")
        warnings = check_focus_grounding(cfg)
        self.assertEqual(len(warnings), 2)


class TestCheckFocusGroundingWarnNeverBlock(unittest.TestCase):
    """check_focus_grounding never raises regardless of focus content."""

    def test_none_focus_does_not_raise(self):
        """A specialist with focus=None (coerced) must not raise."""
        spec = SpecialistSpec(role="scan", provider="xai", model="grok",
                              focus="", toolset=["web"])
        # Manually set focus to None to test the defensive coerce.
        spec.focus = None  # type: ignore[assignment]
        synthesis = SynthesisSpec(provider="openrouter", model="google/gemini-3-flash")
        cfg = FleetConfig(name="t", specialists=[spec],
                          synthesis=synthesis, convergence="synthesize")
        try:
            check_focus_grounding(cfg)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"check_focus_grounding raised on None focus: {exc}")


class TestCheckFocusGroundingReadsPersonaInstruction(unittest.TestCase):
    """KTD3: check_focus_grounding reads effective_instruction (which is the persona
    body for persona specs), not raw focus.

    This is the discriminating test for U3: before the change,
    check_focus_grounding read spec.focus directly; a persona spec has
    focus="" so it would always skip the grounding check (false-no-warn).
    After the change, the resolver populates effective_instruction with the
    persona body, and check_focus_grounding reads that.
    """

    def setUp(self):
        import tempfile, shutil
        self._tmp = tempfile.mkdtemp()
        self.pool = os.path.realpath(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp)

    def _write_persona(self, name: str, body: str) -> None:
        with open(os.path.join(self.pool, name + ".md"), "w", encoding="utf-8") as f:
            f.write(body)

    def _persona_config(self, name: str, toolset=None) -> FleetConfig:
        """Build a single-specialist collect config using a named persona."""
        from fleet_engine.personas import resolve as _resolve
        if toolset is None:
            toolset = ["web"]
        cfg = FleetConfig(
            name="test-fleet",
            specialists=[
                SpecialistSpec(
                    role="scan",
                    provider="xai",
                    model="grok-4.3",
                    persona=name,
                    toolset=toolset,
                )
            ],
            synthesis=None,
            convergence="collect",
        )
        _resolve(cfg, self.pool)
        return cfg

    def test_persona_with_anti_grounding_body_warns(self):
        """A retrieval-toolset persona whose body has no sourcing directive → warn.

        This proves the lint reads effective_instruction (the persona body),
        not raw focus (which would be empty for a persona spec).
        """
        self._write_persona(
            "ungrounded-persona",
            "Fast, broad coverage. Breadth over depth. No sourcing required.",
        )
        cfg = self._persona_config("ungrounded-persona", toolset=["web"])
        warnings = check_focus_grounding(cfg)
        self.assertEqual(len(warnings), 1,
                         f"persona without sourcing directive must warn; got: {warnings}")

    def test_persona_with_sourcing_directive_no_warn(self):
        """A retrieval-toolset persona whose body demands citations → no warn.

        Discriminating complement: same persona mechanism but grounded body.
        """
        self._write_persona(
            "grounded-persona",
            "Cite a primary source with a link for every claim. "
            "Mark as unsourced if no source is found.",
        )
        cfg = self._persona_config("grounded-persona", toolset=["web"])
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [],
                         f"persona with sourcing directive must not warn; got: {warnings}")

    def test_persona_on_empty_toolset_not_checked(self):
        """Persona with toolset=[] is not a retrieval lane — grounding check is skipped."""
        self._write_persona(
            "no-tools-persona",
            "Analyze the inputs. No tools, no sourcing needed.",
        )
        cfg = self._persona_config("no-tools-persona", toolset=[])
        warnings = check_focus_grounding(cfg)
        self.assertEqual(warnings, [],
                         "empty-toolset persona lane must not be grounding-checked")


class TestRenderPreviewWarningsWithFocusLint(unittest.TestCase):
    """render_preview_warnings integrates focus-lint into the validation output (U6)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_palette_none_with_focus_failing_fleet_returns_warning_block(self):
        """palette=None + ungrounded retrieval lane → ⚠ block with focus warning AND skipped note.

        This confirms the U6 None-palette branch fires: focus lint runs even when
        the palette is absent, and the output is the ⚠ block (not just the skipped note).
        """
        cfg = _make_config(
            specialist_toolset=["web"],
            specialist_focus=_BEFORE_FOCUS,  # anti-grounding, no sourcing → warn
        )
        missing_palette = self.tmp / "no_palette.yaml"
        result = render_preview_warnings(cfg, palette_path=missing_palette)
        # Must have the warning block header.
        self.assertIn("⚠", result)
        self.assertIn("fleet validation", result)
        # Must still tell the operator about the missing palette.
        self.assertIn("skipped", result.lower())
        # Must contain the focus warning.
        self.assertTrue(
            any(term in result.lower() for term in ("anti-grounding", "sourcing", "cite")),
            f"expected focus warning in output, got: {result}",
        )

    def test_palette_none_grounded_fleet_returns_plain_skipped_note(self):
        """palette=None + grounded retrieval lane → plain skipped note (no ⚠ block)."""
        cfg = _make_config(
            specialist_toolset=["web"],
            specialist_focus=_AFTER_FOCUS,  # has sourcing → no focus warning
        )
        missing_palette = self.tmp / "no_palette.yaml"
        result = render_preview_warnings(cfg, palette_path=missing_palette)
        # No ⚠ block (no focus warnings, no palette warnings).
        self.assertNotIn("⚠", result)
        self.assertIn("skipped", result.lower())

    def test_focus_warning_merged_with_palette_warnings_in_same_block(self):
        """Focus warnings and palette warnings appear in the same ⚠ block."""
        # Palette with no matching models/toolsets → palette warns.
        palette_path = _write_palette(self.tmp, models=[], toolsets=[])
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["web"],
            specialist_focus=_BEFORE_FOCUS,  # no sourcing → focus warns too
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        result = render_preview_warnings(cfg, palette_path=palette_path)
        # Single ⚠ block containing both palette and focus warnings.
        self.assertIn("⚠", result)
        self.assertIn("fleet validation", result)
        self.assertRegex(result, r"\d+ warning")
        # The focus warning and palette warning are both present.
        self.assertTrue(
            any(term in result.lower() for term in ("anti-grounding", "sourcing", "cite")),
            "focus warning should be in the block",
        )
        self.assertTrue(any("not in palette" in result for _ in [1]),
                        "palette warning should also be in the block")

    def test_focus_lint_fires_with_collect_fleet_no_palette(self):
        """Focus lint works for collect-convergence fleets (synthesis=None) with no palette."""
        cfg = _make_config(
            convergence="collect",
            specialist_toolset=["web"],
            specialist_focus="Enumerate the landscape, fast and broad.",  # no sourcing
        )
        missing_palette = self.tmp / "no_palette.yaml"
        result = render_preview_warnings(cfg, palette_path=missing_palette)
        # Focus warning fires.
        self.assertIn("⚠", result)
        self.assertIn("skipped", result.lower())


# ---------------------------------------------------------------------------
# R8 guard (no import of preview_lint inside engine/model_client/config)
# This is verified by grepping, but we add a lightweight import-level check
# to confirm engine.py, model_client.py, config.py do NOT import preview_lint.
# ---------------------------------------------------------------------------


class TestR8EnginePurity(unittest.TestCase):
    """preview_lint must NOT be imported by engine.py, model_client.py, or config.py."""

    def _module_source(self, name: str) -> str:
        import importlib
        spec = importlib.util.find_spec(f"fleet_engine.{name}")
        return Path(spec.origin).read_text(encoding="utf-8")

    def test_engine_does_not_import_preview_lint(self):
        source = self._module_source("engine")
        self.assertNotIn("preview_lint", source)

    def test_model_client_does_not_import_preview_lint(self):
        source = self._module_source("model_client")
        self.assertNotIn("preview_lint", source)

    def test_config_does_not_import_preview_lint(self):
        source = self._module_source("config")
        self.assertNotIn("preview_lint", source)


class TestLoadPaletteNonUtf8(unittest.TestCase):
    """A non-UTF-8 / binary palette degrades to None (UnicodeDecodeError caught) — never crashes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_non_utf8_palette_returns_none(self):
        path = self.tmp / "binary.yaml"
        path.write_bytes(b"\xff\xfe\x00\x01 not utf-8 \x80\x81")
        self.assertIsNone(load_palette(str(path)))

    def test_nul_byte_path_returns_none(self):
        # Path()/expanduser() raises ValueError on an embedded NUL — must degrade to None,
        # not crash (the never-raises contract). Copilot finding.
        self.assertIsNone(load_palette("/tmp/\x00bad-palette.yaml"))


class TestPreviewWarningsSanitized(unittest.TestCase):
    """Fleet-controlled strings are sanitized in lint warnings — the preview/validate output
    is a human-approval surface, so a tampered fleet must not inject terminal escapes into it
    (parallels render._sanitize; see sanitize-trust-surface learning)."""

    def test_escape_in_model_stripped_from_palette_warning(self):
        cfg = _make_focus_config(focus="cite a primary source with a link", toolset=["web"])
        cfg.specialists[0].model = "m\x1b[2Kevil"  # off-palette model carrying an ESC
        warnings = check_palette(cfg, Palette(models=set(), toolsets={"web"}))
        self.assertTrue(warnings, "off-palette model should warn")
        self.assertNotIn("\x1b", "\n".join(warnings))

    def test_escape_in_role_stripped_from_focus_warning(self):
        cfg = _make_focus_config(focus="overview of the area", toolset=["web"])  # no sourcing → warns
        cfg.specialists[0].role = "scan\x1b[2J"
        warnings = check_focus_grounding(cfg)
        self.assertTrue(warnings, "ungrounded retrieval lane should warn")
        self.assertNotIn("\x1b", "\n".join(warnings))

    def test_env_palette_path_sanitized_in_skipped_note(self):
        """A CADRE_PALETTE path with a newline/escape can't forge a line on the approval surface."""
        cfg = _make_focus_config(focus="cite a source with a link", toolset=["web"])  # lint-clean
        evil = "/tmp/nope\n[cadre] forged line\x1b[2K"
        with patch.dict(os.environ, {"CADRE_PALETTE": evil}):
            out = render_preview_warnings(cfg)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\r", out)
        # The embedded newline is stripped, so no standalone forged "[cadre] forged line".
        forged = [ln for ln in out.split("\n") if ln.strip() == "[cadre] forged line"]
        self.assertEqual(forged, [])

    def test_nul_byte_path_arg_does_not_crash(self):
        """A malformed palette_path (embedded NUL → ValueError from Path) degrades, never crashes.

        Note: CADRE_PALETTE itself can't carry a NUL (os.environ rejects it at set time), so the
        reachable vector is an explicit palette_path arg — the never-raises contract holds for it.
        """
        cfg = _make_focus_config(focus="cite a source with a link", toolset=["web"])  # lint-clean
        out = render_preview_warnings(cfg, palette_path="/tmp/\x00bad")  # must not raise
        self.assertIsInstance(out, str)
        self.assertIn("validation skipped", out)


if __name__ == "__main__":
    unittest.main()
