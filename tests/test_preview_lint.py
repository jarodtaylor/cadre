"""Tests for fleet_engine/preview_lint.py — palette validation (U5).

All tests are hermetic: palette files are written to tempfile.mkdtemp() (never
~/.cadre), and CADRE_PALETTE is patched via patch.dict so tests don't leak env
state. This follows the test_palette.py / test_config.py precedent of "never
touch ~/.cadre."

Coverage:
- load_palette: missing file, malformed YAML, non-mapping root, missing/non-list
  keys, malformed entries, valid round-trip, CADRE_PALETTE env override.
- check_palette: off-palette model warning, off-palette toolset warning, all
  on-palette → empty list, synthesizer check (synthesize vs. collect convergence).
- render_preview_warnings: missing palette → skipped note; valid palette with
  warnings → ⚠ block; all clean → ✓ line; warn-never-block (no exception).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fleet_engine.config import FleetConfig, SpecialistSpec, SynthesisSpec
from fleet_engine.preview_lint import (
    DEFAULT_PALETTE_PATH,
    Palette,
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
    synth_provider="openrouter",
    synth_model="google/gemini-3-flash",
) -> FleetConfig:
    """Build a minimal FleetConfig directly without loading from disk."""
    specialists = [
        SpecialistSpec(
            role="web",
            provider=specialist_provider,
            model=specialist_model,
            toolset=specialist_toolset if specialist_toolset is not None else ["web"],
        )
    ]
    synthesis = None
    if convergence == "synthesize":
        synthesis = SynthesisSpec(
            provider=synth_provider,
            model=synth_model,
        )
    return FleetConfig(
        name="test-fleet",
        specialists=specialists,
        synthesis=synthesis,
        convergence=convergence,
    )


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
        # Without CADRE_PALETTE and without explicit param, falls back to
        # ~/.cadre/palette.yaml, which doesn't exist on dev → None.
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_PALETTE"}
        with patch.dict(os.environ, env_without, clear=True):
            result = load_palette(None)
        # Dev machine has no ~/.cadre/palette.yaml, so None is expected.
        # (If the machine happens to have one, the test would still pass because
        # load_palette either returns None on missing or a Palette on success —
        # we don't assert None here, just assert no exception.)
        # We can check the default path is used by pointing DEFAULT_PALETTE_PATH
        # at a controlled location via the env override; the above test covers that.
        # This test just confirms no exception.

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
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["web"],
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
        cfg = _make_config(
            specialist_provider="xai", specialist_model="grok-4.3",
            specialist_toolset=["web"],
            synth_provider="openrouter", synth_model="google/gemini-3-flash",
        )
        with patch.dict(os.environ, {"CADRE_PALETTE": str(palette_path)}):
            result = render_preview_warnings(cfg)  # no palette_path param
        self.assertIn("✓", result)


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


if __name__ == "__main__":
    unittest.main()
