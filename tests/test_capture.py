"""Tests for fleet_engine.capture.save_run (U2).

All tests inject a tempfile.mkdtemp() run_dir and never touch ~/.cadre.
Fixtures build FleetResult/AgentResult directly — no live model calls.
"""

import json
import os
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fleet_engine.capture import (
    _safe_role,
    _specialist_md,
    _synthesis_md,
    lane_filename_map,
    prepare_run_dir,
    resolve_run_dir,
    round_subdir,
    save_lane,
    save_prompt,
    save_run,
)
from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult, FleetStatus, run_fleet
from fleet_engine.model_client import AgentResult
from fleet_engine.personas import resolve
from fleet_engine.progress_runner import run_with_progress
from fleet_engine.render import render_result
from tests.test_engine import RoundAwareFakeClient, _derive_status, _iterative_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg(**overrides):
    """Minimal valid FleetConfig; override per test."""
    data = {
        "name": "test-swarm",
        "synthesis": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "google/gemini-3-flash",
             "toolset": ["web"], "focus": "find sources"},
            {"role": "social", "provider": "xai", "model": "grok-4.3",
             "toolset": ["x_search"], "focus": "scan X"},
        ],
    }
    data.update(overrides)
    return FleetConfig.from_dict(data)


def _lane(role="web", provider="openrouter", model="google/gemini-3-flash",
          ok=True, text=None, error=None, elapsed_s=1.23,
          toolset=None, timed_out=False) -> AgentResult:
    """Build an AgentResult with U1 capture fields populated."""
    r = AgentResult(
        role=role,
        provider=provider,
        model=model,
        ok=ok,
        text=text if text is not None else (f"{role}-output" if ok else None),
        error=error if error is not None else (None if ok else f"{role}-error"),
        elapsed_s=elapsed_s,
        toolset=list(toolset) if toolset is not None else [],
        timed_out=timed_out,
    )
    return r


def _result(task="What is the best AI design tool?",
            fleet="test-swarm",
            specialists=None,
            synthesis="Final synthesized report.",
            ok=True,
            synth_ok=True,
            notes=None) -> FleetResult:
    """Build a FleetResult with defaults; override per test."""
    if specialists is None:
        specialists = [
            _lane("web", toolset=["web"]),
            _lane("social", provider="xai", model="grok-4.3", toolset=["x_search"]),
        ]
    return FleetResult(
        fleet=fleet,
        task=task,
        specialists=specialists,
        synthesis=synthesis,
        synth_ok=synth_ok,
        notes=notes or [],
        status=_derive_status(ok, synth_ok, None, "synthesize"),
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestSaveRunWritesAllArtifacts(unittest.TestCase):
    """save_run writes synthesis.md + manifest.json (U3 split: per-lane files written by save_lane)."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_prompt_txt_not_written_by_save_run(self):
        """save_run no longer writes prompt.txt — that moved to the edge (R2, U4)."""
        r = _result()
        save_run(_cfg(), r, self.run_dir)
        self.assertFalse((self.run_dir / "prompt.txt").exists())

    def test_one_specialist_file_per_specialist(self):
        """Per-lane .md files are written by save_lane, not save_run."""
        r = _result()
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        for lane in r.specialists:
            save_lane(lane, fmap[lane.role], self.run_dir)
        self.assertTrue((self.run_dir / "specialist-web.md").exists())
        self.assertTrue((self.run_dir / "specialist-social.md").exists())

    def test_synthesis_md_written(self):
        r = _result()
        save_run(_cfg(), r, self.run_dir)
        self.assertTrue((self.run_dir / "synthesis.md").exists())

    def test_manifest_json_written(self):
        r = _result()
        save_run(_cfg(), r, self.run_dir)
        self.assertTrue((self.run_dir / "manifest.json").exists())

    def test_synthesis_md_contains_synthesis_text(self):
        r = _result(synthesis="My final synthesis.")
        save_run(_cfg(), r, self.run_dir)
        content = (self.run_dir / "synthesis.md").read_text(encoding="utf-8")
        self.assertIn("My final synthesis.", content)


class TestManifestSchema(unittest.TestCase):
    """manifest.json has the required run-level and per-lane keys."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)
        self.cfg = _cfg()
        self.result = _result(
            task="Find design tools",
            fleet="test-swarm",
            specialists=[
                _lane("web", toolset=["web"], elapsed_s=2.5),
                _lane("social", provider="xai", model="grok-4.3",
                      toolset=["x_search"], elapsed_s=3.1),
            ],
        )
        save_run(self.cfg, self.result, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            self.manifest = json.load(f)

    def test_run_level_fleet_key(self):
        self.assertEqual(self.manifest["fleet"], "test-swarm")

    def test_run_level_task_key(self):
        self.assertEqual(self.manifest["task"], "Find design tools")

    def test_run_level_timestamp_present(self):
        self.assertIn("timestamp", self.manifest)
        self.assertIsInstance(self.manifest["timestamp"], str)
        self.assertTrue(len(self.manifest["timestamp"]) > 0)

    def test_run_level_models_list(self):
        models = self.manifest["models"]
        self.assertIsInstance(models, list)
        providers = [m["provider"] for m in models]
        self.assertIn("openrouter", providers)
        self.assertIn("xai", providers)

    def test_run_level_synth_ok(self):
        self.assertTrue(self.manifest["synth_ok"])

    def test_run_level_synthesizer(self):
        synth = self.manifest["synthesizer"]
        self.assertEqual(synth["provider"], self.cfg.synthesis.provider)
        self.assertEqual(synth["model"], self.cfg.synthesis.model)

    def test_run_level_hermes_home_key_present(self):
        self.assertIn("hermes_home", self.manifest)

    def test_lanes_list_present(self):
        self.assertIn("lanes", self.manifest)
        self.assertIsInstance(self.manifest["lanes"], list)
        self.assertEqual(len(self.manifest["lanes"]), 2)

    def test_lane_u1_fields_present(self):
        lane = next(lane for lane in self.manifest["lanes"] if lane["role"] == "web")
        self.assertIn("role", lane)
        self.assertIn("provider", lane)
        self.assertIn("model", lane)
        self.assertIn("ok", lane)
        self.assertIn("error", lane)
        self.assertIn("elapsed_s", lane)
        self.assertIn("toolset", lane)
        self.assertIn("timed_out", lane)

    def test_lane_elapsed_s_value(self):
        lane = next(lane for lane in self.manifest["lanes"] if lane["role"] == "web")
        self.assertAlmostEqual(lane["elapsed_s"], 2.5, places=5)

    def test_lane_toolset_list(self):
        lane = next(lane for lane in self.manifest["lanes"] if lane["role"] == "web")
        self.assertEqual(lane["toolset"], ["web"])

    def test_synthesizer_not_a_lane_entry(self):
        """Synthesizer appears at run-level only (synth_ok), not in lanes."""
        roles = [lane["role"] for lane in self.manifest["lanes"]]
        self.assertNotIn("synthesizer", roles)


class TestNoToolsetSpecialistSerializesEmptyList(unittest.TestCase):
    """A specialist with no toolset must serialize as "toolset": [] — never null or missing."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_empty_toolset_serializes_as_empty_list(self):
        r = _result(specialists=[_lane("notool", toolset=[])])
        save_run(_cfg(specialists=[
            {"role": "notool", "provider": "openrouter", "model": "m", "focus": "analysis only"},
        ]), r, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        lane = manifest["lanes"][0]
        self.assertIn("toolset", lane)
        self.assertEqual(lane["toolset"], [])
        self.assertIsNotNone(lane["toolset"])


class TestFailedSpecialist(unittest.TestCase):
    """A failed specialist writes its error to a specialist file; folder is still complete."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_failed_specialist_file_written(self):
        """save_lane writes the file for a failed specialist (error text preserved)."""
        failed = _lane("social", ok=False, error="auth error", toolset=["x_search"])
        r = _result(specialists=[_lane("web", toolset=["web"]), failed])
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        save_lane(failed, fmap["social"], self.run_dir)
        save_run(_cfg(), r, self.run_dir)
        social_path = self.run_dir / "specialist-social.md"
        self.assertTrue(social_path.exists())
        content = social_path.read_text(encoding="utf-8")
        self.assertIn("auth error", content)

    def test_failed_specialist_error_in_manifest(self):
        failed = _lane("social", ok=False, error="auth error", toolset=["x_search"])
        r = _result(specialists=[_lane("web", toolset=["web"]), failed])
        save_run(_cfg(), r, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        lane = next(lane for lane in manifest["lanes"] if lane["role"] == "social")
        self.assertFalse(lane["ok"])
        self.assertEqual(lane["error"], "auth error")

    def test_folder_still_complete_with_failed_specialist(self):
        """save_run writes synthesis.md + manifest.json; per-lane files are save_lane's job."""
        failed = _lane("social", ok=False, error="auth error", toolset=["x_search"])
        r = _result(specialists=[_lane("web", toolset=["web"]), failed])
        save_run(_cfg(), r, self.run_dir)
        for fname in ("synthesis.md", "manifest.json"):
            self.assertTrue((self.run_dir / fname).exists(), f"missing: {fname}")


class TestTimedOutSpecialist(unittest.TestCase):
    """A timed-out specialist's manifest entry has timed_out: true and an elapsed_s."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_timed_out_lane_in_manifest(self):
        timed_out_lane = _lane(
            "social", ok=False, error="timed out after 600s",
            elapsed_s=600.1, toolset=["x_search"], timed_out=True,
        )
        r = _result(specialists=[_lane("web", toolset=["web"]), timed_out_lane])
        save_run(_cfg(), r, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        lane = next(lane for lane in manifest["lanes"] if lane["role"] == "social")
        self.assertTrue(lane["timed_out"])
        self.assertIsNotNone(lane["elapsed_s"])
        self.assertGreater(lane["elapsed_s"], 0)

    def test_non_timed_out_lane_has_timed_out_false(self):
        r = _result(specialists=[_lane("web", toolset=["web"], timed_out=False)])
        save_run(_cfg(specialists=[
            {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"],
             "focus": "web research"},
        ]), r, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        lane = manifest["lanes"][0]
        self.assertFalse(lane["timed_out"])


class TestHermesHome(unittest.TestCase):
    """manifest records the resolved absolute HERMES_HOME (env-sourced) — expanding
    ~ and making relative values absolute — or the resolved default when unset."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _load_manifest(self):
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            return json.load(f)

    def test_hermes_home_recorded_when_set(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/custom/hermes"}):
            save_run(_cfg(), _result(), self.run_dir)
        manifest = self._load_manifest()
        self.assertEqual(manifest["hermes_home"], "/custom/hermes")

    def test_hermes_home_default_when_unset_is_resolved(self):
        env_without_hermes = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        with patch.dict(os.environ, env_without_hermes, clear=True):
            save_run(_cfg(), _result(), self.run_dir)
        manifest = self._load_manifest()
        expected = os.path.abspath(os.path.expanduser("~/.hermes"))
        self.assertEqual(manifest["hermes_home"], expected)
        self.assertTrue(os.path.isabs(manifest["hermes_home"]))

    def test_hermes_home_tilde_is_expanded_to_absolute(self):
        # The core of #7: a ~ value must resolve to an absolute path so the operator
        # sees the real profile directory, not an unexpanded "~/...".
        with patch.dict(os.environ, {"HERMES_HOME": "~/some-profile"}):
            save_run(_cfg(), _result(), self.run_dir)
        manifest = self._load_manifest()
        expected = os.path.abspath(os.path.expanduser("~/some-profile"))
        self.assertEqual(manifest["hermes_home"], expected)
        self.assertNotIn("~", manifest["hermes_home"])
        self.assertTrue(os.path.isabs(manifest["hermes_home"]))

    def test_hermes_home_relative_is_made_absolute(self):
        with patch.dict(os.environ, {"HERMES_HOME": "rel/profile"}):
            save_run(_cfg(), _result(), self.run_dir)
        manifest = self._load_manifest()
        self.assertTrue(os.path.isabs(manifest["hermes_home"]))
        self.assertTrue(manifest["hermes_home"].endswith(os.path.join("rel", "profile")))

    def test_hermes_home_empty_falls_back_to_default(self):
        # Set-but-empty HERMES_HOME must read as the default, not the cwd — the
        # preview/manifest exist to tell the operator which profile is in play.
        with patch.dict(os.environ, {"HERMES_HOME": ""}):
            save_run(_cfg(), _result(), self.run_dir)
        manifest = self._load_manifest()
        expected = os.path.abspath(os.path.expanduser("~/.hermes"))
        self.assertEqual(manifest["hermes_home"], expected)


class TestFullyFailedRun(unittest.TestCase):
    """A fully-failed run (synthesis None, synth_ok None) still writes the folder."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_folder_complete_on_all_specialists_failed(self):
        """save_run writes synthesis.md + manifest.json even when all specialists failed."""
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        r = _result(specialists=specialists, synthesis=None, ok=False, synth_ok=None)
        save_run(_cfg(), r, self.run_dir)
        for fname in ("synthesis.md", "manifest.json"):
            self.assertTrue((self.run_dir / fname).exists(), f"missing: {fname}")

    def test_synthesis_md_notes_failure_count_when_synth_not_attempted(self):
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        r = _result(specialists=specialists, synthesis=None, ok=False, synth_ok=None)
        save_run(_cfg(), r, self.run_dir)
        content = (self.run_dir / "synthesis.md").read_text(encoding="utf-8")
        # Should note that N of M specialists failed and synthesis was not attempted
        self.assertIn("2 of 2", content)
        self.assertIn("specialist", content)

    def test_synthesis_md_notes_synthesizer_failure_when_synth_ran_and_failed(self):
        """When specialists succeeded but synthesizer failed, synthesis.md says so."""
        specialists = [_lane("web", toolset=["web"])]
        r = _result(
            specialists=specialists,
            synthesis=None,
            ok=False,
            synth_ok=False,
            notes=["synthesizer failed: rate limited"],
        )
        save_run(_cfg(specialists=[
            {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"],
             "focus": "web research"},
        ]), r, self.run_dir)
        content = (self.run_dir / "synthesis.md").read_text(encoding="utf-8")
        # Should note that the synthesizer failed, not "N of M specialists failed"
        self.assertIn("synthesizer failed", content)

    def test_manifest_synth_ok_is_none_when_not_attempted(self):
        specialists = [_lane("web", ok=False, error="down", toolset=["web"])]
        r = _result(specialists=specialists, synthesis=None, ok=False, synth_ok=None)
        save_run(_cfg(specialists=[
            {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"],
             "focus": "web research"},
        ]), r, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertIsNone(manifest["synth_ok"])


class TestFilePermissions(unittest.TestCase):
    """All artifact files are written owner-only (0o600)."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_specialist_files_are_0o600(self):
        """save_lane writes per-lane files owner-only (0o600)."""
        r = _result()
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        for lane in r.specialists:
            save_lane(lane, fmap[lane.role], self.run_dir)
        for fname in ("specialist-web.md", "specialist-social.md"):
            mode = stat.S_IMODE((self.run_dir / fname).stat().st_mode)
            self.assertEqual(mode, 0o600, f"{fname} should be 0o600")

    def test_synthesis_md_is_0o600(self):
        save_run(_cfg(), _result(), self.run_dir)
        mode = stat.S_IMODE((self.run_dir / "synthesis.md").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_manifest_json_is_0o600(self):
        save_run(_cfg(), _result(), self.run_dir)
        mode = stat.S_IMODE((self.run_dir / "manifest.json").stat().st_mode)
        self.assertEqual(mode, 0o600)


class TestRunDirInjection(unittest.TestCase):
    """Every write lands in the injected run_dir — nothing touches ~/.cadre."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_all_artifacts_under_injected_run_dir(self):
        """save_run writes only synthesis.md + manifest.json (prompt.txt is U4's job)."""
        save_run(_cfg(), _result(), self.run_dir)
        artifacts = list(self.run_dir.iterdir())
        names = {p.name for p in artifacts}
        self.assertIn("manifest.json", names)
        self.assertIn("synthesis.md", names)
        self.assertNotIn("prompt.txt", names)
        # All artifacts are immediate children of run_dir (or subdirs thereof)
        for p in artifacts:
            self.assertTrue(str(p).startswith(str(self.run_dir)))

    def test_save_run_creates_run_dir_if_missing(self):
        nested = self.run_dir / "nested" / "leaf"
        self.assertFalse(nested.exists())
        save_run(_cfg(), _result(), nested)
        self.assertTrue(nested.exists())
        self.assertTrue((nested / "manifest.json").exists())


# ---------------------------------------------------------------------------
# FIX 1: _safe_role and slash-in-role filename safety
# ---------------------------------------------------------------------------


class TestSafeRole(unittest.TestCase):
    """_safe_role sanitizes roles for filenames while preserving case."""

    def test_plain_role_unchanged(self):
        self.assertEqual(_safe_role("web"), "web")

    def test_slash_replaced(self):
        self.assertEqual(_safe_role("web/scraper"), "web-scraper")

    def test_dotdot_replaced(self):
        # "../escape": '.', '.', '/' all become '-' → "---escape"
        self.assertEqual(_safe_role("../escape"), "---escape")

    def test_case_preserved(self):
        # 'Web' and 'web' must NOT collide
        self.assertEqual(_safe_role("Web"), "Web")
        self.assertNotEqual(_safe_role("Web"), _safe_role("web"))

    def test_empty_role_falls_back_to_unknown(self):
        self.assertEqual(_safe_role(""), "unknown")

    def test_only_special_chars_falls_back_to_unknown(self):
        # "/" alone sanitizes to "-", not empty — so result is "-", not "unknown"
        self.assertEqual(_safe_role("/"), "-")

    def test_underscore_and_dash_preserved(self):
        self.assertEqual(_safe_role("my_role-v2"), "my_role-v2")


class TestSlashInRoleWritesSafeFile(unittest.TestCase):
    """A role containing '/' writes a safe filename inside run_dir; manifest keeps true role."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_slash_role_file_inside_run_dir(self):
        """specialist with role='web/evil' writes inside run_dir, not as a subpath escape."""
        r = _result(specialists=[_lane("web/evil", toolset=[])])
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        save_lane(r.specialists[0], fmap["web/evil"], self.run_dir)
        save_run(_cfg(specialists=[
            {"role": "web/evil", "provider": "openrouter", "model": "m", "focus": "analysis"},
        ]), r, self.run_dir)
        # The sanitized filename must be inside run_dir
        expected_file = self.run_dir / "specialist-web-evil.md"
        self.assertTrue(expected_file.exists(), "sanitized file should exist inside run_dir")
        # No subdir 'web' should have been created
        self.assertFalse((self.run_dir / "web").is_dir(), "'web' subdir must not be created")

    def test_dotdot_role_file_inside_run_dir(self):
        """specialist with role='../escape' writes inside run_dir."""
        r = _result(specialists=[_lane("../escape", toolset=[])])
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        save_lane(r.specialists[0], fmap["../escape"], self.run_dir)
        save_run(_cfg(specialists=[
            {"role": "../escape", "provider": "openrouter", "model": "m", "focus": "analysis"},
        ]), r, self.run_dir)
        # No escape outside run_dir
        escaped_path = self.run_dir.parent / "escape.md"
        self.assertFalse(escaped_path.exists(), "file must not escape run_dir via ..")

    def test_slash_role_true_role_in_manifest(self):
        """The manifest's lane.role must be the TRUE role, not the sanitized filename."""
        r = _result(specialists=[_lane("web/evil", toolset=[])])
        save_run(_cfg(specialists=[
            {"role": "web/evil", "provider": "openrouter", "model": "m", "focus": "analysis"},
        ]), r, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["lanes"][0]["role"], "web/evil")

    def test_slash_role_true_role_in_markdown_header(self):
        """The markdown header must use the TRUE role."""
        r = _result(specialists=[_lane("web/evil", toolset=[])])
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        save_lane(r.specialists[0], fmap["web/evil"], self.run_dir)
        content = (self.run_dir / "specialist-web-evil.md").read_text(encoding="utf-8")
        self.assertIn("# Specialist: web/evil", content)

    def test_folder_complete_with_slash_role(self):
        """manifest.json is present after a run with a slash-in-role specialist."""
        r = _result(specialists=[_lane("web/evil", toolset=[])])
        save_run(_cfg(specialists=[
            {"role": "web/evil", "provider": "openrouter", "model": "m", "focus": "analysis"},
        ]), r, self.run_dir)
        self.assertTrue((self.run_dir / "manifest.json").exists())


# ---------------------------------------------------------------------------
# FIX 2: save_run creates run_dir with 0o700
# ---------------------------------------------------------------------------


class TestSaveRunCreatesRunDir0o700(unittest.TestCase):
    """save_run creates a not-yet-existing run_dir with mode 0o700."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_save_run_new_dir_is_0o700(self):
        run_dir = self.tmp / "newleaf"
        self.assertFalse(run_dir.exists())
        save_run(_cfg(), _result(), run_dir)
        self.assertTrue(run_dir.exists())
        mode = stat.S_IMODE(run_dir.stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")


# ---------------------------------------------------------------------------
# resolve_run_dir is now a pure resolver — no FS access, no collision logic
# ---------------------------------------------------------------------------


class TestResolveRunDirPure(unittest.TestCase):
    """resolve_run_dir is a pure Path resolver: no FS access, no collision avoidance."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_resolve_run_dir_does_not_create_directory(self):
        """resolve_run_dir must NOT create the directory it returns."""
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch("fleet_engine.capture._DEFAULT_RUNS_ROOT", str(self.tmp)):
            with patch.dict(os.environ, env_without, clear=True):
                result = resolve_run_dir("pure resolver test")
        self.assertFalse(result.exists(), "resolve_run_dir must not create the directory")

    def test_cadre_run_dir_returned_verbatim(self):
        """CADRE_RUN_DIR is returned verbatim even if it already exists."""
        existing = self.tmp / "fixed-dir"
        existing.mkdir()
        with patch.dict(os.environ, {"CADRE_RUN_DIR": str(existing)}):
            result = resolve_run_dir("collision test")
        self.assertEqual(result, existing)

    def test_same_second_returns_same_base(self):
        """Two calls in the same second return the same base path (no collision logic)."""
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch("fleet_engine.capture._DEFAULT_RUNS_ROOT", str(self.tmp)):
            with patch.dict(os.environ, env_without, clear=True):
                first = resolve_run_dir("collision test")
                second = resolve_run_dir("collision test")
        # Same second → same base leaf (collision handling is prepare_run_dir's job)
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# prepare_run_dir: atomic reservation, explicit path, writability probe
# ---------------------------------------------------------------------------


class TestPrepareRunDirAtomicReservation(unittest.TestCase):
    """prepare_run_dir atomically reserves default-path dirs (no TOCTOU)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_two_calls_same_task_return_distinct_created_dirs(self):
        """Two same-second default-path calls get two distinct, already-created dirs."""
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch("fleet_engine.capture._DEFAULT_RUNS_ROOT", str(self.tmp)):
            with patch.dict(os.environ, env_without, clear=True):
                first = prepare_run_dir("collision test")
                second = prepare_run_dir("collision test")
        # Both must exist (reserved on creation, not just checked)
        self.assertTrue(first.exists(), "first dir must be created")
        self.assertTrue(second.exists(), "second dir must be created")
        # They must be distinct
        self.assertNotEqual(first, second)
        # The second gets the -2 suffix
        self.assertTrue(second.name.endswith("-2"), f"expected -2 suffix, got {second.name!r}")

    def test_first_call_creates_dir(self):
        """prepare_run_dir creates the directory it returns."""
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch("fleet_engine.capture._DEFAULT_RUNS_ROOT", str(self.tmp)):
            with patch.dict(os.environ, env_without, clear=True):
                result = prepare_run_dir("basic creation test")
        self.assertTrue(result.exists())

    def test_default_path_missing_parents_created(self):
        """First-ever run: missing runs-root parent is created (not a FileNotFoundError)."""
        nested_root = self.tmp / "cadre" / "runs"
        # Don't pre-create nested_root — simulate a fresh host
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch("fleet_engine.capture._DEFAULT_RUNS_ROOT", str(nested_root)):
            with patch.dict(os.environ, env_without, clear=True):
                result = prepare_run_dir("first ever run")
        self.assertTrue(result.exists(), "run_dir must be created even when parents are missing")

    def test_explicit_run_dir_is_reused(self):
        """An injected run_dir is reused (exist_ok=True — user controls the path)."""
        explicit = self.tmp / "my-run"
        explicit.mkdir()
        # Pre-existing explicit dir must not raise
        result = prepare_run_dir("task", run_dir=explicit)
        self.assertEqual(result, explicit)

    def test_cadre_run_dir_reused_if_exists(self):
        """CADRE_RUN_DIR already existing is not an error."""
        existing = self.tmp / "fixed"
        existing.mkdir()
        with patch.dict(os.environ, {"CADRE_RUN_DIR": str(existing)}):
            result = prepare_run_dir("task")
        self.assertEqual(result, existing)

    def test_dir_permissions_are_0o700(self):
        """prepare_run_dir creates directories owner-only."""
        import stat as stat_mod
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch("fleet_engine.capture._DEFAULT_RUNS_ROOT", str(self.tmp)):
            with patch.dict(os.environ, env_without, clear=True):
                result = prepare_run_dir("perm test")
        mode = stat_mod.S_IMODE(result.stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses filesystem permissions")
class TestPrepareRunDirWritabilityProbe(unittest.TestCase):
    """prepare_run_dir raises OSError for an existing but unwritable explicit dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # addCleanup runs LIFO: shutil.rmtree added first, chmod added second —
        # so chmod(0o700) runs BEFORE rmtree to ensure rmtree can remove the dir.
        self.addCleanup(shutil.rmtree, self.tmp)

    def _make_readonly(self, name: str) -> Path:
        """Create a subdirectory, make it read-only, and register cleanup."""
        ro_dir = self.tmp / name
        ro_dir.mkdir(mode=0o700)
        os.chmod(ro_dir, 0o500)
        # addCleanup is LIFO: chmod (restore) is registered AFTER rmtree is already
        # registered, so it will run BEFORE rmtree.
        self.addCleanup(os.chmod, ro_dir, 0o700)
        return ro_dir

    def test_read_only_explicit_dir_raises_os_error(self):
        """An injected run_dir that exists but is read-only causes OSError."""
        ro_dir = self._make_readonly("readonly")
        with self.assertRaises(OSError):
            prepare_run_dir("task", run_dir=ro_dir)

    def test_read_only_cadre_run_dir_raises_os_error(self):
        """CADRE_RUN_DIR that exists but is read-only causes OSError."""
        ro_dir = self._make_readonly("readonly-env")
        with patch.dict(os.environ, {"CADRE_RUN_DIR": str(ro_dir)}):
            with self.assertRaises(OSError):
                prepare_run_dir("task")


# ---------------------------------------------------------------------------
# Fix 3: Unique specialist filenames after _safe_role sanitization
# ---------------------------------------------------------------------------


class TestSpecialistFilenameDeduplication(unittest.TestCase):
    """save_run deduplicates specialist filenames when _safe_role produces a collision."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_two_roles_sanitizing_to_same_name_get_distinct_files(self):
        """'a/b' and 'a:b' both sanitize to 'a-b' → files must be specialist-a-b.md and specialist-a-b-2.md."""
        specialists = [
            _lane("a/b", toolset=[]),
            _lane("a:b", toolset=[]),
        ]
        r = _result(specialists=specialists)
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        for lane in r.specialists:
            save_lane(lane, fmap[lane.role], self.run_dir)
        save_run(_cfg(specialists=[
            {"role": "a/b", "provider": "openrouter", "model": "m", "focus": "analysis"},
            {"role": "a:b", "provider": "openrouter", "model": "m", "focus": "synthesis"},
        ]), r, self.run_dir)

        file1 = self.run_dir / "specialist-a-b.md"
        file2 = self.run_dir / "specialist-a-b-2.md"
        self.assertTrue(file1.exists(), "first file specialist-a-b.md must exist")
        self.assertTrue(file2.exists(), "second file specialist-a-b-2.md must exist")

    def test_both_deduped_files_are_non_empty(self):
        """Both deduplicated files must be non-empty (not overwritten)."""
        specialists = [
            _lane("a/b", toolset=[], text="output-ab-slash"),
            _lane("a:b", toolset=[], text="output-ab-colon"),
        ]
        r = _result(specialists=specialists)
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        for lane in r.specialists:
            save_lane(lane, fmap[lane.role], self.run_dir)

        content1 = (self.run_dir / "specialist-a-b.md").read_text(encoding="utf-8")
        content2 = (self.run_dir / "specialist-a-b-2.md").read_text(encoding="utf-8")
        self.assertIn("output-ab-slash", content1)
        self.assertIn("output-ab-colon", content2)

    def test_manifest_lanes_carry_correct_file_values(self):
        """Each manifest lane's 'file' key must match the actual on-disk filename."""
        specialists = [
            _lane("a/b", toolset=[]),
            _lane("a:b", toolset=[]),
        ]
        r = _result(specialists=specialists)
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        for lane in r.specialists:
            save_lane(lane, fmap[lane.role], self.run_dir)
        save_run(_cfg(specialists=[
            {"role": "a/b", "provider": "openrouter", "model": "m", "focus": "analysis"},
            {"role": "a:b", "provider": "openrouter", "model": "m", "focus": "synthesis"},
        ]), r, self.run_dir)

        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)

        lane_ab_slash = next(lane for lane in manifest["lanes"] if lane["role"] == "a/b")
        lane_ab_colon = next(lane for lane in manifest["lanes"] if lane["role"] == "a:b")

        self.assertIn("file", lane_ab_slash)
        self.assertIn("file", lane_ab_colon)
        self.assertEqual(lane_ab_slash["file"], "specialist-a-b.md")
        self.assertEqual(lane_ab_colon["file"], "specialist-a-b-2.md")
        # And the files actually exist (written by save_lane above)
        self.assertTrue((self.run_dir / lane_ab_slash["file"]).exists())
        self.assertTrue((self.run_dir / lane_ab_colon["file"]).exists())

    def test_non_colliding_roles_have_file_key_in_manifest(self):
        """Normal (non-colliding) lanes also have the 'file' key in the manifest."""
        r = _result()
        fmap = lane_filename_map([lane.role for lane in r.specialists])
        for lane in r.specialists:
            save_lane(lane, fmap[lane.role], self.run_dir)
        save_run(_cfg(), r, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        for lane in manifest["lanes"]:
            self.assertIn("file", lane, f"lane {lane['role']!r} missing 'file' key")
            self.assertTrue(lane["file"].endswith(".md"))
            self.assertTrue((self.run_dir / lane["file"]).exists())


# ---------------------------------------------------------------------------
# lane_filename_map: deterministic role→filename mapping (U3)
# ---------------------------------------------------------------------------


class TestLaneFilenameMap(unittest.TestCase):
    """lane_filename_map produces the same filenames save_run previously wrote."""

    def test_normal_roles_produce_specialist_prefix(self):
        """Plain roles map to specialist-<role>.md."""
        fmap = lane_filename_map(["web", "social"])
        self.assertEqual(fmap["web"], "specialist-web.md")
        self.assertEqual(fmap["social"], "specialist-social.md")

    def test_slash_role_is_sanitized_in_filename(self):
        """A slash in a role becomes '-' in the filename; the key stays the true role."""
        fmap = lane_filename_map(["web/evil"])
        self.assertIn("web/evil", fmap)
        self.assertEqual(fmap["web/evil"], "specialist-web-evil.md")

    def test_colliding_roles_get_deduped_filenames(self):
        """Two roles that sanitize to the same stem get -2 suffix on the second."""
        fmap = lane_filename_map(["a/b", "a:b"])
        self.assertEqual(fmap["a/b"], "specialist-a-b.md")
        self.assertEqual(fmap["a:b"], "specialist-a-b-2.md")

    def test_map_is_injective(self):
        """No two roles map to the same filename."""
        roles = ["web", "social", "a/b", "a:b", "scan"]
        fmap = lane_filename_map(roles)
        filenames = list(fmap.values())
        self.assertEqual(len(filenames), len(set(filenames)), "filenames must be unique")

    def test_map_is_order_stable(self):
        """Roles are processed in config order; dedup suffix reflects that order."""
        fmap = lane_filename_map(["a:b", "a/b"])
        # Now a:b arrives first and gets the plain name; a/b gets -2
        self.assertEqual(fmap["a:b"], "specialist-a-b.md")
        self.assertEqual(fmap["a/b"], "specialist-a-b-2.md")

    def test_empty_roles_returns_empty_map(self):
        self.assertEqual(lane_filename_map([]), {})

    def test_single_role_returns_single_entry(self):
        fmap = lane_filename_map(["scan"])
        self.assertEqual(fmap, {"scan": "specialist-scan.md"})


# ---------------------------------------------------------------------------
# save_lane: per-lane writer called on LaneDone (U3)
# ---------------------------------------------------------------------------


class TestSaveLane(unittest.TestCase):
    """save_lane writes each specialist's .md the moment its lane finishes."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_save_lane_writes_file(self):
        """save_lane writes the specialist markdown file at the pre-mapped filename."""
        lane = _lane("web", text="web output", toolset=["web"])
        save_lane(lane, "specialist-web.md", self.run_dir)
        self.assertTrue((self.run_dir / "specialist-web.md").exists())

    def test_save_lane_file_is_0o600(self):
        """save_lane writes the file owner-only (0o600)."""
        lane = _lane("web", toolset=["web"])
        save_lane(lane, "specialist-web.md", self.run_dir)
        mode = stat.S_IMODE((self.run_dir / "specialist-web.md").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_save_lane_true_role_in_header(self):
        """The markdown header uses the TRUE role, not the safe filename stem."""
        lane = _lane("web/evil", toolset=[])
        save_lane(lane, "specialist-web-evil.md", self.run_dir)
        content = (self.run_dir / "specialist-web-evil.md").read_text(encoding="utf-8")
        self.assertIn("# Specialist: web/evil", content)

    def test_save_lane_failed_lane_writes_error_content(self):
        """A failed lane's error text lands in its .md file."""
        lane = _lane("social", ok=False, error="rate limit", toolset=[])
        save_lane(lane, "specialist-social.md", self.run_dir)
        content = (self.run_dir / "specialist-social.md").read_text(encoding="utf-8")
        self.assertIn("rate limit", content)

    def test_save_lane_two_distinct_files_do_not_collide(self):
        """Two save_lane calls with pre-mapped distinct filenames do not overwrite each other."""
        lane_ab = _lane("a/b", text="output-ab-slash", toolset=[])
        lane_colon = _lane("a:b", text="output-ab-colon", toolset=[])
        fmap = lane_filename_map(["a/b", "a:b"])
        save_lane(lane_ab, fmap["a/b"], self.run_dir)
        save_lane(lane_colon, fmap["a:b"], self.run_dir)
        content1 = (self.run_dir / "specialist-a-b.md").read_text(encoding="utf-8")
        content2 = (self.run_dir / "specialist-a-b-2.md").read_text(encoding="utf-8")
        self.assertIn("output-ab-slash", content1)
        self.assertIn("output-ab-colon", content2)

    def test_save_lane_creates_run_dir_if_missing(self):
        """save_lane creates run_dir if it doesn't yet exist (crash-resilience, R11)."""
        nested = self.run_dir / "not-yet" / "leaf"
        self.assertFalse(nested.exists())
        lane = _lane("web", toolset=[])
        save_lane(lane, "specialist-web.md", nested)
        self.assertTrue(nested.exists())
        self.assertTrue((nested / "specialist-web.md").exists())

    def test_partial_run_leaves_only_written_lanes(self):
        """Writing only 2 of 3 lanes leaves exactly those 2 files (crash-resilience, R11)."""
        lanes = [
            _lane("web", text="web-out", toolset=[]),
            _lane("social", text="social-out", toolset=[]),
            _lane("scan", text="scan-out", toolset=[]),
        ]
        fmap = lane_filename_map(["web", "social", "scan"])
        # Write only the first two (simulate a crash before the third)
        save_lane(lanes[0], fmap["web"], self.run_dir)
        save_lane(lanes[1], fmap["social"], self.run_dir)
        self.assertTrue((self.run_dir / "specialist-web.md").exists())
        self.assertTrue((self.run_dir / "specialist-social.md").exists())
        self.assertFalse((self.run_dir / "specialist-scan.md").exists())


# ---------------------------------------------------------------------------
# cli.py: unwritable dir fails fast before model calls (writability probe)
# ---------------------------------------------------------------------------


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses filesystem permissions")
class TestRunCommandReadOnlyDirFailsFast(unittest.TestCase):
    """run_command with an existing-but-unwritable run_dir exits non-zero, makes ZERO model calls."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # addCleanup is LIFO: rmtree registered first, chmod second → chmod runs before rmtree.
        self.addCleanup(shutil.rmtree, self.tmp)

    def _make_readonly(self, name: str) -> Path:
        ro_dir = self.tmp / name
        ro_dir.mkdir(mode=0o700)
        os.chmod(ro_dir, 0o500)
        self.addCleanup(os.chmod, ro_dir, 0o700)
        return ro_dir

    def test_read_only_injected_dir_returns_nonzero(self):
        from fleet_engine.cli import run_command
        ro_dir = self._make_readonly("readonly")

        from tests.test_cli import FakeClient
        client = FakeClient({"synthesizer": ("ok", "SYNTH")})
        code, _out = run_command(EXAMPLE, "task", client=client, run_dir=ro_dir)

        self.assertNotEqual(code, 0, "should return non-zero for unwritable dir")

    def test_read_only_injected_dir_makes_zero_model_calls(self):
        from fleet_engine.cli import run_command
        ro_dir = self._make_readonly("readonly2")

        from tests.test_cli import FakeClient
        client = FakeClient({"synthesizer": ("ok", "SYNTH")})
        run_command(EXAMPLE, "task", client=client, run_dir=ro_dir)

        self.assertEqual(len(client.calls), 0, "no model calls should be made (fail-fast)")


EXAMPLE = "fleets/research-swarm.example.yaml"


# ---------------------------------------------------------------------------
# save_prompt (U4): prompt.txt is written by the edge up front, owner-only.
# (Replaces the coverage of the removed save_run prompt.txt write.)
# ---------------------------------------------------------------------------


class TestSavePrompt(unittest.TestCase):
    """save_prompt writes prompt.txt with the task, 0o600, creating run_dir if missing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_writes_task_content(self):
        save_prompt(self.tmp, "my exact task")
        self.assertEqual((self.tmp / "prompt.txt").read_text(encoding="utf-8"), "my exact task")

    def test_prompt_txt_is_0o600(self):
        save_prompt(self.tmp, "task")
        mode = stat.S_IMODE((self.tmp / "prompt.txt").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_creates_run_dir_if_missing(self):
        nested = self.tmp / "nested" / "leaf"
        self.assertFalse(nested.exists())
        save_prompt(nested, "task")
        self.assertTrue((nested / "prompt.txt").exists())


# ---------------------------------------------------------------------------
# U4 (fleet-shapes): collect convergence — manifest, synthesis.md, distinguishability
# ---------------------------------------------------------------------------


def _collect_cfg(**overrides):
    """Minimal valid collect FleetConfig (no synthesis block required)."""
    data = {
        "name": "test-collect",
        "convergence": "collect",
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "google/gemini-3-flash",
             "toolset": ["web"], "focus": "find sources"},
            {"role": "social", "provider": "xai", "model": "grok-4.3",
             "toolset": ["x_search"], "focus": "scan X"},
        ],
    }
    data.update(overrides)
    return FleetConfig.from_dict(data)


def _collect_result(specialists=None, ok=True, text_suffix="") -> FleetResult:
    """Build a collect FleetResult (convergence='collect', synthesis=None, synth_ok=None)."""
    if specialists is None:
        specialists = [
            _lane("web", toolset=["web"], text=f"web-output{text_suffix}"),
            _lane("social", provider="xai", model="grok-4.3", toolset=["x_search"],
                  text=f"social-output{text_suffix}"),
        ]
    return FleetResult(
        fleet="test-collect",
        task="Find design tools",
        specialists=specialists,
        synthesis=None,
        synth_ok=None,
        notes=[],
        convergence="collect",
        status=_derive_status(ok, None, None, "collect"),
    )


class TestCollectManifest(unittest.TestCase):
    """Collect run manifest carries convergence='collect', synthesizer=None, synth_ok=None."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _load_manifest(self):
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            return json.load(f)

    def test_collect_manifest_convergence_field(self):
        """manifest.convergence == 'collect' for a collect run."""
        save_run(_collect_cfg(), _collect_result(), self.run_dir)
        manifest = self._load_manifest()
        self.assertEqual(manifest["convergence"], "collect")

    def test_collect_manifest_synthesizer_is_none(self):
        """manifest.synthesizer is null (None) for a collect run."""
        save_run(_collect_cfg(), _collect_result(), self.run_dir)
        manifest = self._load_manifest()
        self.assertIsNone(manifest["synthesizer"])

    def test_collect_manifest_synth_ok_is_none(self):
        """manifest.synth_ok is null (None) for a collect run (synthesis never ran)."""
        save_run(_collect_cfg(), _collect_result(), self.run_dir)
        manifest = self._load_manifest()
        self.assertIsNone(manifest["synth_ok"])


class TestCollectSynthesisMd(unittest.TestCase):
    """synthesis.md for a collect run contains attributed specialist blocks, not 'was not attempted'."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _synthesis_content(self, result):
        save_run(_collect_cfg(), result, self.run_dir)
        return (self.run_dir / "synthesis.md").read_text(encoding="utf-8")

    def test_collect_success_synthesis_md_does_not_say_not_attempted(self):
        """Negative assertion: a successful collect run's synthesis.md must NOT say 'synthesis was not attempted'."""
        content = self._synthesis_content(_collect_result(ok=True))
        self.assertNotIn("synthesis was not attempted", content)

    def test_collect_success_synthesis_md_contains_attributed_blocks(self):
        """A successful collect run's synthesis.md has the collected-output header and per-specialist blocks."""
        content = self._synthesis_content(_collect_result(ok=True))
        self.assertIn("Collected specialist outputs", content)

    def test_collect_success_synthesis_md_attributes_each_specialist(self):
        """Each specialist's role appears in the synthesis.md attributed blocks."""
        content = self._synthesis_content(_collect_result(ok=True))
        self.assertIn("web", content)
        self.assertIn("social", content)

    def test_collect_success_synthesis_md_contains_specialist_text(self):
        """The specialist output text appears verbatim in the attributed blocks."""
        content = self._synthesis_content(_collect_result(ok=True, text_suffix="-unique-text"))
        self.assertIn("web-output-unique-text", content)
        self.assertIn("social-output-unique-text", content)

    def test_collect_all_failed_synthesis_md_collect_mode_note(self):
        """All-failed collect run's synthesis.md says 'collect mode', NOT 'synthesis was not attempted'."""
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        result = FleetResult(
            fleet="test-collect", task="t", specialists=specialists,
            synthesis=None, synth_ok=None, convergence="collect",
            status=FleetStatus.FAILED,
        )
        content = self._synthesis_content(result)
        self.assertNotIn("synthesis was not attempted", content)
        self.assertIn("collect mode", content)

    def test_collect_all_failed_synthesis_md_failure_count(self):
        """All-failed collect run's synthesis.md names the failure count."""
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        result = FleetResult(
            fleet="test-collect", task="t", specialists=specialists,
            synthesis=None, synth_ok=None, convergence="collect",
            status=FleetStatus.FAILED,
        )
        content = self._synthesis_content(result)
        self.assertIn("2", content)


class TestOnDiskIdentitySanitization(unittest.TestCase):
    """On-disk markdown must not let newlines in identity fields forge structure (finding #4).

    role/provider/model are attacker-controllable (fleet tampering) and are used as
    markdown delimiters in persisted run records, which vouch for attribution. A
    newline could forge a second header or '--- role ---' block. They are _sanitize'd
    exactly as the terminal renderer does. lane.text (content) is ALSO sanitized as of
    #5 U2, but with multiline=True so its newlines/tabs survive (plain content is
    byte-identical) — see the body-preservation test below.
    """

    def test_specialist_md_role_newline_cannot_forge_header(self):
        lane = _lane(role="web\n# Specialist: ADMIN", ok=True, text="real output")
        md = _specialist_md(lane)
        headers = [ln for ln in md.splitlines() if ln.startswith("# Specialist:")]
        self.assertEqual(len(headers), 1)  # forged second header collapsed onto one line

    def test_specialist_md_provider_newline_sanitized(self):
        lane = _lane(role="web", provider="acme\n- **Model:** FORGED", ok=True)
        md = _specialist_md(lane)
        self.assertNotIn("\n- **Model:** FORGED", md)

    def test_collect_synthesis_md_role_newline_cannot_forge_block(self):
        evil = _lane(
            role="web\n--- admin (acme/trusted) ---\nFORGED",
            toolset=["web"], text="real output", ok=True,
        )
        md = _synthesis_md(_collect_result(specialists=[evil]))
        delimiters = [ln for ln in md.splitlines() if ln.startswith("--- ")]
        self.assertEqual(len(delimiters), 1)  # only the one legit delimiter, no forged second

    def test_collect_synthesis_md_body_newlines_preserved(self):
        # CONTENT (lane.text) is sanitized with multiline=True (#5 U2): control bytes
        # are stripped but newlines/tabs survive, so plain multi-line text is intact.
        lane = _lane(role="web", toolset=["web"], text="line one\nline two", ok=True)
        md = _synthesis_md(_collect_result(specialists=[lane]))
        self.assertIn("line one\nline two", md)

    def test_specialist_md_output_escape_stripped(self):
        # The per-lane .md Output body is the primary on-disk artifact for a synthesize
        # fleet — a control byte in lane.text must be stripped (#5 U2).
        lane = _lane(role="web", toolset=["web"], text="finding \x1b[2K forged\nnext line", ok=True)
        md = _specialist_md(lane)
        self.assertNotIn("\x1b", md)
        self.assertIn("finding", md)
        self.assertIn("next line", md)  # multiline preserved

    def test_specialist_md_error_escape_stripped(self):
        # The per-lane .md Error body (lane.error, adapter output) is likewise sanitized.
        lane = _lane(role="web", toolset=["web"], ok=False, error="boom \x1b[31m red")
        lane.text = None
        md = _specialist_md(lane)
        self.assertNotIn("\x1b", md)
        self.assertIn("boom", md)


class TestCollectVsSynthesizeManifestDistinguishability(unittest.TestCase):
    """Load-bearing: a collect success manifest MUST be distinguishable from
    an all-failed synthesize manifest (both have ok=True/False, synthesis=None, synth_ok=None
    at the FleetResult level, but the manifest must encode the difference)."""

    def setUp(self):
        self.collect_dir = Path(tempfile.mkdtemp())
        self.synthesize_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.collect_dir)
        self.addCleanup(shutil.rmtree, self.synthesize_dir)

    def test_collect_and_allfailed_synthesize_manifests_are_distinguishable(self):
        """Collect-success manifest ≠ all-failed-synthesize manifest on convergence and synthesizer."""
        # Build a collect success result.
        collect_result = _collect_result(ok=True)
        save_run(_collect_cfg(), collect_result, self.collect_dir)
        with open(self.collect_dir / "manifest.json", encoding="utf-8") as f:
            collect_manifest = json.load(f)

        # Build an all-failed synthesize result.
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        synth_result = FleetResult(
            fleet="test-swarm", task="Find design tools", specialists=specialists,
            synthesis=None, synth_ok=None,
            convergence="synthesize",
            status=FleetStatus.FAILED,
        )
        save_run(_cfg(), synth_result, self.synthesize_dir)
        with open(self.synthesize_dir / "manifest.json", encoding="utf-8") as f:
            synth_manifest = json.load(f)

        # convergence field distinguishes them.
        self.assertEqual(collect_manifest["convergence"], "collect")
        self.assertEqual(synth_manifest["convergence"], "synthesize")
        self.assertNotEqual(collect_manifest["convergence"], synth_manifest["convergence"])

        # synthesizer field distinguishes them: collect=None, synthesize=dict.
        self.assertIsNone(collect_manifest["synthesizer"])
        self.assertIsNotNone(synth_manifest["synthesizer"])
        self.assertIsInstance(synth_manifest["synthesizer"], dict)


# ---------------------------------------------------------------------------
# Judge convergence fixtures (U5)
# ---------------------------------------------------------------------------

# Per-run judge marker nonce (R5, #5): the engine sets result.judge_marker_nonce
# and the parser requires it, so judge fixtures carry it in their markers.
_JUDGE_NONCE = "cap7x2ab"

# Canonical two-lane judge text that parse_grades can fully parse.
_JUDGE_TEXT_FULL = (
    f"=== LANE: web {_JUDGE_NONCE} ===\nGrade: A\nRationale: Excellent grounding.\n\n"
    f"=== LANE: social {_JUDGE_NONCE} ===\nGrade: B\nRationale: Good social coverage."
)


def _judge_cfg(**overrides):
    """Minimal valid judge FleetConfig (no synthesis block required)."""
    data = {
        "name": "test-judge",
        "convergence": "judge",
        "judge": {"provider": "openrouter", "model": "judge/model"},
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "google/gemini-3-flash",
             "toolset": ["web"], "focus": "find sources"},
            {"role": "social", "provider": "xai", "model": "grok-4.3",
             "toolset": ["x_search"], "focus": "scan X"},
        ],
    }
    data.update(overrides)
    return FleetConfig.from_dict(data)


def _judge_result(
    judge=_JUDGE_TEXT_FULL,
    judge_ok=True,
    ok=True,
    specialists=None,
    notes=None,
) -> FleetResult:
    """Build a judge FleetResult (convergence='judge', synthesis=None, synth_ok=None)."""
    if specialists is None:
        specialists = [
            _lane("web", toolset=["web"]),
            _lane("social", provider="xai", model="grok-4.3", toolset=["x_search"]),
        ]
    return FleetResult(
        fleet="test-judge",
        task="Score design tools",
        specialists=specialists,
        synthesis=None,
        synth_ok=None,
        notes=notes or [],
        convergence="judge",
        status=_derive_status(ok, None, judge_ok, "judge"),
        judge=judge,
        judge_ok=judge_ok,
        judge_marker_nonce=_JUDGE_NONCE,
    )


# ---------------------------------------------------------------------------
# Judge: synthesis.md content (U5)
# ---------------------------------------------------------------------------


class TestJudgeSynthesisMd(unittest.TestCase):
    """synthesis.md for a judge run contains the judge grade text + attributed blocks, or degrade note."""

    def _md(self, result: FleetResult) -> str:
        return _synthesis_md(result)

    # --- Success path ---

    def test_judge_success_md_not_failure_note(self):
        """NEGATIVE: a successful judge run's synthesis.md must NOT say 'synthesis was not attempted'."""
        md = self._md(_judge_result())
        self.assertNotIn("synthesis was not attempted", md)
        self.assertNotIn("all 2 specialists failed", md)

    def test_judge_success_md_has_judge_grade_header(self):
        """synthesis.md for a successful judge run starts with '# Judge grade'."""
        md = self._md(_judge_result())
        self.assertTrue(md.startswith("# Judge grade"), repr(md[:80]))

    def test_judge_success_md_judge_text_present(self):
        """The raw judge text appears verbatim in synthesis.md."""
        md = self._md(_judge_result())
        # Spot-check canonical sub-strings from the judge text
        self.assertIn("Grade: A", md)
        self.assertIn("Grade: B", md)
        self.assertIn("Excellent grounding", md)

    def test_judge_success_md_specialist_blocks_present(self):
        """Attributed specialist blocks appear after the judge grade section."""
        md = self._md(_judge_result())
        self.assertIn("# Specialist outputs", md)
        self.assertIn("--- web", md)
        self.assertIn("--- social", md)

    def test_judge_success_md_specialist_text_preserved(self):
        """Specialist lane.text (content) is preserved verbatim in the specialist blocks."""
        lane = _lane("web", toolset=["web"], text="unique-web-content")
        result = _judge_result(specialists=[lane,
                                            _lane("social", provider="xai",
                                                  model="grok-4.3", toolset=["x_search"])])
        md = self._md(result)
        self.assertIn("unique-web-content", md)

    # --- Degrade path ---

    def test_judge_degrade_md_degrade_note(self):
        """Judge degrade (judge_ok False) writes a degrade note, not the all-fail message."""
        result = _judge_result(judge=None, judge_ok=False, ok=False,
                               notes=["judge failed: rate limited"])
        md = self._md(result)
        self.assertNotIn("synthesis was not attempted", md)
        self.assertNotIn("all 2 specialists failed", md)
        self.assertIn("No judge grade", md)

    def test_judge_degrade_md_contains_note_text(self):
        """The degrade note extracts the 'judge failed' message from result.notes."""
        result = _judge_result(judge=None, judge_ok=False, ok=False,
                               notes=["judge failed: rate limited"])
        md = self._md(result)
        self.assertIn("judge failed: rate limited", md)

    def test_judge_degrade_md_fallback_when_no_note(self):
        """When notes contains no 'judge failed' entry, falls back to 'judge failed'."""
        result = _judge_result(judge=None, judge_ok=False, ok=False, notes=[])
        md = self._md(result)
        self.assertIn("judge failed", md)

    def test_judge_degrade_note_not_confused_by_specialist_error(self):
        """A specialist note that merely contains the phrase 'judge failed' must NOT be
        picked as the degrade reason. Specialist notes are appended before the judge note,
        so a loose substring match would mis-report; the prefix match selects the real one."""
        result = _judge_result(
            judge=None, judge_ok=False, ok=False,
            notes=[
                "specialist 'web' failed: upstream said judge failed to respond",
                "judge failed: rate limited",
            ],
        )
        md = self._md(result)
        self.assertIn("judge failed: rate limited", md)
        self.assertNotIn("upstream said judge failed to respond", md)

    # --- All-fail path ---

    def test_judge_all_failed_md_says_judge_mode(self):
        """All specialists failed (judge_ok None) → note says 'judge mode', not 'collect mode'."""
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        result = _judge_result(judge=None, judge_ok=None, ok=False,
                               specialists=specialists)
        md = self._md(result)
        self.assertIn("judge mode", md)
        self.assertNotIn("collect mode", md)
        self.assertNotIn("synthesis was not attempted", md)

    def test_judge_all_failed_md_failure_count(self):
        """All-fail judge synthesis.md names the specialist count."""
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        result = _judge_result(judge=None, judge_ok=None, ok=False,
                               specialists=specialists)
        md = self._md(result)
        self.assertIn("2", md)


# ---------------------------------------------------------------------------
# Judge: manifest (U5)
# ---------------------------------------------------------------------------


class TestJudgeManifest(unittest.TestCase):
    """Judge run manifest carries convergence='judge', judge={provider/model}, judge_ok, grades, ungraded."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _load_manifest(self, cfg=None, result=None):
        cfg = cfg or _judge_cfg()
        result = result or _judge_result()
        save_run(cfg, result, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            return json.load(f)

    def test_judge_manifest_convergence_field(self):
        """manifest.convergence == 'judge' for a judge run."""
        manifest = self._load_manifest()
        self.assertEqual(manifest["convergence"], "judge")

    def test_judge_manifest_synthesizer_is_none(self):
        """manifest.synthesizer is null for a judge run (no synthesis step)."""
        manifest = self._load_manifest()
        self.assertIsNone(manifest["synthesizer"])

    def test_judge_manifest_synth_ok_is_none(self):
        """manifest.synth_ok is null for a judge run (synthesis never ran)."""
        manifest = self._load_manifest()
        self.assertIsNone(manifest["synth_ok"])

    def test_judge_manifest_judge_field_present(self):
        """manifest.judge carries provider and model from cfg.judge."""
        cfg = _judge_cfg()
        manifest = self._load_manifest(cfg=cfg)
        self.assertIsNotNone(manifest["judge"])
        self.assertEqual(manifest["judge"]["provider"], cfg.judge.provider)
        self.assertEqual(manifest["judge"]["model"], cfg.judge.model)

    def test_judge_manifest_judge_ok_true(self):
        """manifest.judge_ok == True on a successful judge run."""
        manifest = self._load_manifest(result=_judge_result(judge_ok=True))
        self.assertIs(manifest["judge_ok"], True)

    def test_judge_manifest_grades_list(self):
        """manifest.grades is a list of per-lane grade dicts."""
        manifest = self._load_manifest()
        grades = manifest["grades"]
        self.assertIsInstance(grades, list)
        self.assertGreater(len(grades), 0)
        for g in grades:
            for key in ("role", "model", "grade", "rationale"):
                self.assertIn(key, g)

    def test_judge_manifest_ungraded_empty_on_full_coverage(self):
        """manifest.ungraded is [] when all surviving lanes are graded."""
        manifest = self._load_manifest()
        self.assertEqual(manifest["ungraded"], [])

    def test_judge_manifest_grades_role_values(self):
        """manifest.grades entries carry the correct role values."""
        manifest = self._load_manifest()
        graded_roles = {g["role"] for g in manifest["grades"]}
        self.assertIn("web", graded_roles)
        self.assertIn("social", graded_roles)

    def test_judge_manifest_partial_coverage_ungraded(self):
        """Partial coverage: ungraded lists the lane whose role was not graded."""
        # Only "web" graded — "social" will land in ungraded.
        partial_judge_text = f"=== LANE: web {_JUDGE_NONCE} ===\nGrade: A\nRationale: Good."
        result = _judge_result(judge=partial_judge_text, judge_ok=True)
        manifest = self._load_manifest(result=result)
        self.assertEqual(len(manifest["grades"]), 1)
        self.assertEqual(manifest["grades"][0]["role"], "web")
        ungraded_roles = {u["role"] for u in manifest["ungraded"]}
        self.assertIn("social", ungraded_roles)

    def test_judge_manifest_parse_failure_fields(self):
        """When parse_grades returns parsed_ok=False and judge text exists → parse_failed + judge_text_raw."""
        garbled_judge = "This is not a parseable judge output at all."
        result = _judge_result(judge=garbled_judge, judge_ok=True)
        manifest = self._load_manifest(result=result)
        self.assertIs(manifest.get("parse_failed"), True)
        self.assertEqual(manifest.get("judge_text_raw"), garbled_judge)
        self.assertEqual(manifest["grades"], [])  # nothing extracted

    def test_judge_manifest_empty_success_marked_parse_failed(self):
        """A 'successful' judge (judge_ok True) that returned empty text must still be
        flagged parse_failed — otherwise grades=[] + all-ungraded is indistinguishable
        from a real partial-coverage run (bot review). Defense-in-depth on the contract."""
        result = _judge_result(judge="", judge_ok=True)
        manifest = self._load_manifest(result=result)
        self.assertIs(manifest.get("parse_failed"), True)
        self.assertEqual(manifest.get("judge_text_raw"), "")
        self.assertEqual(manifest["grades"], [])

    def test_judge_manifest_degrade_judge_ok_false(self):
        """Degrade run manifest has judge_ok=False and empty grades/ungraded.

        The fixture has two SUCCESSFUL specialists, so a naive parse_grades over the
        (None) judge text would list both as ungraded — making a judge-FAILURE read
        like a partial-coverage run. Assert ungraded is empty: no grading was
        attempted, so the partial-coverage signal must stay silent.
        """
        result = _judge_result(judge=None, judge_ok=False, ok=False,
                               notes=["judge failed: rate limited"])
        manifest = self._load_manifest(result=result)
        self.assertIs(manifest["judge_ok"], False)
        self.assertEqual(manifest["grades"], [])
        self.assertEqual(manifest["ungraded"], [])
        self.assertNotIn("parse_failed", manifest)

    def test_judge_manifest_all_failed_judge_ok_none(self):
        """All-fail run manifest has judge_ok=None."""
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        result = _judge_result(judge=None, judge_ok=None, ok=False,
                               specialists=specialists)
        manifest = self._load_manifest(result=result)
        self.assertIsNone(manifest["judge_ok"])

    def test_synthesize_manifest_judge_is_none(self):
        """A synthesize-mode run's manifest has judge=null and judge_ok=null (no judge step)."""
        save_run(_cfg(), _result(), self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertIsNone(manifest["judge"])
        self.assertIsNone(manifest["judge_ok"])

    def test_collect_manifest_judge_is_none(self):
        """A collect-mode run's manifest has judge=null and judge_ok=null."""
        save_run(_collect_cfg(), _collect_result(), self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertIsNone(manifest["judge"])
        self.assertIsNone(manifest["judge_ok"])


# ---------------------------------------------------------------------------
# Judge: save_run does not raise (U5)
# ---------------------------------------------------------------------------


class TestJudgeSaveRunNoRaise(unittest.TestCase):
    """save_run on a judge fleet must not raise (test the 3-way convergence dispatch)."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_save_run_judge_success_no_raise(self):
        """save_run on a successful judge fleet completes without exception."""
        save_run(_judge_cfg(), _judge_result(), self.run_dir)
        self.assertTrue((self.run_dir / "manifest.json").exists())

    def test_save_run_judge_degrade_no_raise(self):
        """save_run on a degraded judge fleet (judge_ok=False) completes without exception."""
        result = _judge_result(judge=None, judge_ok=False, ok=False,
                               notes=["judge failed: rate limited"])
        save_run(_judge_cfg(), result, self.run_dir)
        self.assertTrue((self.run_dir / "manifest.json").exists())

    def test_save_run_judge_all_failed_no_raise(self):
        """save_run on an all-fail judge fleet (judge_ok=None) completes without exception."""
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        result = _judge_result(judge=None, judge_ok=None, ok=False,
                               specialists=specialists)
        save_run(_judge_cfg(), result, self.run_dir)
        self.assertTrue((self.run_dir / "manifest.json").exists())


# ---------------------------------------------------------------------------
# Judge: grade text is written UNMUTATED on disk (KTD8, U5)
# ---------------------------------------------------------------------------


class TestJudgeGradeSanitizedOnDisk(unittest.TestCase):
    """The judge grade text is sanitized before it reaches disk (#5 U2, reverses KTD8).

    A persisted synthesis.md is a trust surface a human/agent reads back, so it must be
    as un-spoofable as the terminal render (which already sanitizes judge text). A
    terminal escape in the judge text must NOT survive in synthesis.md.
    """

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_terminal_escape_stripped_in_synthesis_md(self):
        """A terminal escape in the judge text is stripped on disk (#5 U2 — reverses KTD8).

        The on-disk record is a trust surface a human/agent reads back, so it must
        be as un-spoofable as the terminal render (which already sanitizes judge text).
        """
        escape = "\x1b[31m"
        judge_text_with_escape = (
            f"{escape}=== LANE: web ===\nGrade: A\nRationale: {escape}Bold finding."
        )
        result = _judge_result(judge=judge_text_with_escape)
        save_run(_judge_cfg(), result, self.run_dir)
        on_disk = (self.run_dir / "synthesis.md").read_text(encoding="utf-8")
        self.assertNotIn(escape, on_disk,
                         "#5 U2: capture must sanitize the judge text; the ESC byte must be gone")
        self.assertIn("Bold finding.", on_disk)  # visible content survives


# ---------------------------------------------------------------------------
# Judge: end-to-end smoke test (U5)
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal duck-typed ModelClient for the smoke test — mirrors test_engine.FakeClient."""

    def __init__(self, behavior=None):
        self.behavior = behavior or {}

    def run(self, *, role, provider, model, prompt, toolset=()):
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        # Nonce-aware judge fake: a real judge reads the per-run marker nonce from
        # its prompt and reproduces it. Model that by substituting the nonce we can
        # read out of the prompt for the <NONCE> placeholder in the payload (R5, #5).
        if ok and payload and "<NONCE>" in payload:
            m = re.search(r"=== LANE: \S+ ([0-9a-f]+) ===", prompt)
            assert m, "fake judge could not find the marker nonce in its prompt"
            payload = payload.replace("<NONCE>", m.group(1))
        return AgentResult(
            role=role, provider=provider, model=model, ok=ok,
            text=payload if ok else None, error=None if ok else payload,
        )


class TestJudgeEndToEnd(unittest.TestCase):
    """Smoke: run_fleet → render_result + save_run — wires engine, capture, and render together.

    Uses a FakeClient that returns canonical-format judge text so parse_grades produces
    structured entries.  Validates both the terminal render path and the on-disk capture
    path in one test, catching any edge-wiring break introduced by U2–U5.
    """

    # Canonical judge text: both lanes graded → parse_grades returns 2 entries, 0
    # ungraded. <NONCE> is substituted with the run's actual marker nonce by the
    # nonce-aware _FakeClient (mirrors a real judge reproducing the nonce, R5 #5).
    _JUDGE_RESPONSE = (
        "=== LANE: web <NONCE> ===\nGrade: A\nRationale: Strong grounding.\n\n"
        "=== LANE: social <NONCE> ===\nGrade: B\nRationale: Adequate social coverage."
    )

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _build_cfg(self):
        cfg = _judge_cfg()
        resolve(cfg, "/unused")  # focus-only: sets effective_instruction, zero I/O
        return cfg

    def _run(self):
        cfg = self._build_cfg()
        client = _FakeClient({
            "web": ("ok", "web-specialist-output"),
            "social": ("ok", "social-specialist-output"),
            "judge": ("ok", self._JUDGE_RESPONSE),
        })
        return cfg, run_fleet(cfg, "smoke task", client)

    def test_e2e_result_judge_ok(self):
        """run_fleet returns judge_ok=True and the raw judge text."""
        _, result = self._run()
        self.assertIs(result.judge_ok, True)
        # The fake judge reproduced the run's nonce, so result.judge carries the
        # real marker (not the <NONCE> placeholder) with the grades intact.
        self.assertNotIn("<NONCE>", result.judge)
        self.assertIn("Grade: A", result.judge)
        self.assertIsNotNone(result.judge_marker_nonce)
        self.assertIn(f"=== LANE: web {result.judge_marker_nonce} ===", result.judge)
        self.assertEqual(result.convergence, "judge")

    def test_e2e_render_result_contains_judge_text(self):
        """render_result shows the judge text content (no '# Judge grade' header — render is different from capture)."""
        _, result = self._run()
        rendered = render_result(result)
        # render_result leads with sanitized judge text; check content sub-strings
        self.assertIn("Grade: A", rendered)
        self.assertIn("Strong grounding", rendered)

    def test_e2e_save_run_no_raise(self):
        """save_run completes without exception on the live run_fleet result."""
        cfg, result = self._run()
        save_run(cfg, result, self.run_dir)
        self.assertTrue((self.run_dir / "synthesis.md").exists())
        self.assertTrue((self.run_dir / "manifest.json").exists())

    def test_e2e_synthesis_md_has_judge_grade_header(self):
        """synthesis.md starts with '# Judge grade' (capture boundary, not render)."""
        cfg, result = self._run()
        save_run(cfg, result, self.run_dir)
        content = (self.run_dir / "synthesis.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# Judge grade"), repr(content[:80]))

    def test_e2e_manifest_grades_fully_structured(self):
        """manifest.grades has one entry per specialist; ungraded is empty."""
        cfg, result = self._run()
        save_run(cfg, result, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(len(manifest["grades"]), 2)
        self.assertEqual(manifest["ungraded"], [])
        graded_roles = {g["role"] for g in manifest["grades"]}
        self.assertEqual(graded_roles, {"web", "social"})

    def test_e2e_manifest_judge_ok_true(self):
        """manifest.judge_ok == True on the end-to-end run."""
        cfg, result = self._run()
        save_run(cfg, result, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertIs(manifest["judge_ok"], True)


class TestRunTitleRename(unittest.TestCase):
    """save_run records the synthesizer's H1 as a manifest ``title`` and renames a
    default ``<stamp>-<slug>`` run folder to ``<stamp>-<title-slug>`` (#4)."""

    _STAMP = "2026-06-29-150719"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        # These exercise DEFAULT mode (rename fires only when CADRE_RUN_DIR is unset);
        # make that independent of the ambient env. The CADRE_RUN_DIR test re-sets it.
        env_patch = patch.dict(os.environ)
        env_patch.start()
        os.environ.pop("CADRE_RUN_DIR", None)
        self.addCleanup(env_patch.stop)

    def _stamped(self, leaf):
        d = self.root / leaf
        d.mkdir()
        return d

    def _manifest(self, run_dir):
        return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    def test_h1_renames_dir_and_sets_manifest_title(self):
        run_dir = self._stamped(f"{self._STAMP}-what-is-best")
        final = save_run(_cfg(), _result(synthesis="# Best AI Design Tool\n\nBody."), run_dir)
        self.assertEqual(final.name, f"{self._STAMP}-best-ai-design-tool")
        self.assertFalse(run_dir.exists())             # old leaf is gone
        self.assertEqual(self._manifest(final)["title"], "Best AI Design Tool")

    def test_no_h1_keeps_slug_and_null_title(self):
        run_dir = self._stamped(f"{self._STAMP}-some-task")
        final = save_run(_cfg(), _result(synthesis="Just prose, no heading."), run_dir)
        self.assertEqual(final, run_dir)
        self.assertIsNone(self._manifest(final)["title"])

    def test_failed_synthesis_keeps_slug_and_null_title(self):
        run_dir = self._stamped(f"{self._STAMP}-some-task")
        result = _result(synthesis=None, ok=False, synth_ok=False)
        final = save_run(_cfg(), result, run_dir)
        self.assertEqual(final, run_dir)
        self.assertIsNone(self._manifest(final)["title"])

    def test_cadre_run_dir_set_never_renames_but_still_titles(self):
        run_dir = self._stamped(f"{self._STAMP}-some-task")
        with patch.dict(os.environ, {"CADRE_RUN_DIR": str(self.root)}):
            final = save_run(_cfg(), _result(synthesis="# A Real Title\n\nBody."), run_dir)
        self.assertEqual(final, run_dir)               # caller owns the path → no rename
        self.assertEqual(self._manifest(final)["title"], "A Real Title")

    def test_rename_collision_appends_counter(self):
        run_dir = self._stamped(f"{self._STAMP}-orig")
        (self.root / f"{self._STAMP}-a-real-title").mkdir()   # pre-existing target
        final = save_run(_cfg(), _result(synthesis="# A Real Title\n\nBody."), run_dir)
        self.assertEqual(final.name, f"{self._STAMP}-a-real-title-2")

    def test_non_stamped_leaf_is_not_renamed(self):
        run_dir = self.root / "not-a-stamped-leaf"
        run_dir.mkdir()
        final = save_run(_cfg(), _result(synthesis="# A Real Title\n\nBody."), run_dir)
        self.assertEqual(final, run_dir)

    def test_path_like_h1_cannot_escape_runs_root(self):
        run_dir = self._stamped(f"{self._STAMP}-orig")
        final = save_run(_cfg(), _result(synthesis="# ../../etc/passwd\n\nBody."), run_dir)
        self.assertEqual(final.parent, self.root)      # stays under the runs root
        self.assertNotIn("..", final.name)             # _slugify defanged it
        self.assertTrue(final.name.startswith(f"{self._STAMP}-"))

    def test_rename_failure_degrades_to_original_dir(self):
        run_dir = self._stamped(f"{self._STAMP}-orig")
        result = _result(synthesis="# A Real Title\n\nBody.")
        with patch.object(Path, "rename", side_effect=OSError("cross-device")):
            final = save_run(_cfg(), result, run_dir)
        self.assertEqual(final, run_dir)               # degrade: keep the completed run
        self.assertTrue((run_dir / "manifest.json").exists())

    def test_stamp_regex_matches_real_prepare_run_dir_leaf(self):
        """Coupling guard: the rename's stamp prefix must match what prepare_run_dir
        actually produces, so the regex can't silently drift from the strftime format."""
        from fleet_engine.capture import _STAMP_RE
        with patch("fleet_engine.capture._DEFAULT_RUNS_ROOT", str(self.root)):
            created = prepare_run_dir("a real task title")
        self.assertIsNotNone(
            _STAMP_RE.match(created.name),
            f"_STAMP_RE must match a real prepare_run_dir leaf: {created.name}",
        )


# ---------------------------------------------------------------------------
# Sequential+collect chain: manifest fields + per-lane skipped markdown (U5)
# ---------------------------------------------------------------------------


def _chain_cfg(**overrides):
    """Minimal sequential+collect FleetConfig (scout → analyst → writer)."""
    data = {
        "name": "test-chain",
        "convergence": "collect",
        "topology": "sequential",
        "specialists": [
            {"role": "scout", "provider": "openrouter", "model": "s/m",
             "toolset": ["web"], "focus": "gather sources"},
            {"role": "analyst", "provider": "xai", "model": "grok",
             "toolset": ["web"], "focus": "audit and verify"},
            {"role": "writer", "provider": "openrouter", "model": "w/m",
             "toolset": [], "focus": "write prose"},
        ],
    }
    data.update(overrides)
    return FleetConfig.from_dict(data)


def _skipped_lane(role, provider, model) -> AgentResult:
    """Build a skipped-lane AgentResult (chain never ran this lane).

    Constructed directly — not via _lane() — because _lane's error-defaulting
    sets error=f"{role}-error" for any ok=False lane even when error= is not
    supplied, and a skipped lane must have error=None (no call was made).
    toolset=[] is explicit to satisfy the []-vs-None invariant in _build_manifest.
    """
    return AgentResult(
        role=role, provider=provider, model=model,
        ok=False, skipped=True, error=None, elapsed_s=None, toolset=[],
    )


class TestSequentialChainManifest(unittest.TestCase):
    """Sequential+collect chain manifest carries topology/terminal_produced/
    threading_truncated and per-lane skipped — additive; parallel path unaffected."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _load_manifest(self, cfg, result):
        save_run(cfg, result, self.run_dir)
        with open(self.run_dir / "manifest.json", encoding="utf-8") as f:
            return json.load(f)

    def _success_result(self):
        """All 3 lanes succeed — topology=sequential, terminal_produced=True."""
        return FleetResult(
            fleet="test-chain",
            task="research task",
            specialists=[
                _lane("scout", provider="openrouter", model="s/m", toolset=["web"]),
                _lane("analyst", provider="xai", model="grok", toolset=["web"]),
                _lane("writer", provider="openrouter", model="w/m", toolset=[]),
            ],
            synthesis=None,
            synth_ok=None,
            convergence="collect",
            topology="sequential",
            terminal_produced=True,
            threading_truncated=False,
            status=FleetStatus.SUCCESS,
        )

    def test_chain_success_manifest_topology(self):
        """A sequential chain SUCCESS manifest carries topology='sequential'."""
        manifest = self._load_manifest(_chain_cfg(), self._success_result())
        self.assertEqual(manifest["topology"], "sequential")

    def test_chain_success_manifest_terminal_produced_true(self):
        """Chain SUCCESS: terminal_produced=True in manifest."""
        manifest = self._load_manifest(_chain_cfg(), self._success_result())
        self.assertIs(manifest["terminal_produced"], True)

    def test_chain_success_manifest_threading_truncated_false(self):
        """Chain SUCCESS with no truncation: threading_truncated=False in manifest."""
        manifest = self._load_manifest(_chain_cfg(), self._success_result())
        self.assertFalse(manifest["threading_truncated"])

    def test_chain_success_all_lanes_not_skipped(self):
        """All lanes in a SUCCESS chain have skipped=False in the manifest."""
        manifest = self._load_manifest(_chain_cfg(), self._success_result())
        self.assertEqual(len(manifest["lanes"]), 3)
        for lane in manifest["lanes"]:
            self.assertFalse(lane["skipped"])

    def test_chain_mid_break_lane_states(self):
        """Mid-break DEGRADED: scout ok, analyst real-failure, writer skipped.

        Proves manifest distinguishes a skipped lane (ok=False, skipped=True)
        from a real failure (ok=False, skipped=False) — the entire reason the
        field exists.
        """
        # Analyst fails (ran and failed); writer was never dispatched (skipped).
        result = FleetResult(
            fleet="test-chain",
            task="research task",
            specialists=[
                _lane("scout", provider="openrouter", model="s/m", toolset=["web"]),
                _lane("analyst", provider="xai", model="grok", ok=False,
                      error="rate-limited", toolset=["web"]),
                _skipped_lane("writer", "openrouter", "w/m"),
            ],
            synthesis=None,
            synth_ok=None,
            convergence="collect",
            topology="sequential",
            terminal_produced=False,
            threading_truncated=False,
            # Status set explicitly — _derive_status shim is parallel-only and
            # returns FAILED for (ok=False, synth_ok=None, judge_ok=None, "collect").
            status=FleetStatus.DEGRADED,
        )
        manifest = self._load_manifest(_chain_cfg(), result)

        scout = next(lane for lane in manifest["lanes"] if lane["role"] == "scout")
        analyst = next(lane for lane in manifest["lanes"] if lane["role"] == "analyst")
        writer = next(lane for lane in manifest["lanes"] if lane["role"] == "writer")

        # Scout succeeded
        self.assertTrue(scout["ok"])
        self.assertFalse(scout["skipped"])

        # Analyst ran and failed (real failure — skipped must be False)
        self.assertFalse(analyst["ok"])
        self.assertFalse(analyst["skipped"])

        # Writer never ran (skipped=True — distinguishable from analyst's real failure)
        self.assertFalse(writer["ok"])
        self.assertTrue(writer["skipped"])

    def test_chain_manifest_terminal_produced_false_when_broken(self):
        """A broken chain has terminal_produced=False in the manifest."""
        result = FleetResult(
            fleet="test-chain",
            task="research task",
            specialists=[
                _lane("scout", provider="openrouter", model="s/m", toolset=["web"]),
                _lane("analyst", provider="xai", model="grok", ok=False,
                      error="e", toolset=["web"]),
                _skipped_lane("writer", "openrouter", "w/m"),
            ],
            synthesis=None, synth_ok=None, convergence="collect",
            topology="sequential", terminal_produced=False,
            threading_truncated=False, status=FleetStatus.DEGRADED,
        )
        manifest = self._load_manifest(_chain_cfg(), result)
        self.assertIs(manifest["terminal_produced"], False)

    # --- Regression guards: parallel path must be unaffected ---

    def test_parallel_manifest_topology_parallel(self):
        """Parallel result still has topology='parallel' (additive — no regression)."""
        manifest = self._load_manifest(_cfg(), _result())
        self.assertEqual(manifest["topology"], "parallel")

    def test_parallel_manifest_terminal_produced_null(self):
        """Parallel result: terminal_produced=null (None means not-a-chain)."""
        manifest = self._load_manifest(_cfg(), _result())
        self.assertIsNone(manifest["terminal_produced"])

    def test_parallel_manifest_threading_truncated_false(self):
        """Parallel result: threading_truncated=False."""
        manifest = self._load_manifest(_cfg(), _result())
        self.assertFalse(manifest["threading_truncated"])

    def test_parallel_manifest_lanes_not_skipped(self):
        """Parallel result: every lane has skipped=False in the manifest."""
        manifest = self._load_manifest(_cfg(), _result())
        for lane in manifest["lanes"]:
            self.assertFalse(
                lane["skipped"],
                f"parallel lane {lane['role']!r} must have skipped=False",
            )


class TestSkippedLaneMarkdown(unittest.TestCase):
    """_specialist_md for a skipped lane renders ## Skipped, not ## Error."""

    @staticmethod
    def _skipped(role="writer", provider="openrouter", model="w/m") -> AgentResult:
        return AgentResult(
            role=role, provider=provider, model=model,
            ok=False, skipped=True, error=None, elapsed_s=None, toolset=[],
        )

    def test_skipped_lane_renders_skipped_section(self):
        md = _specialist_md(self._skipped())
        self.assertIn("## Skipped", md)
        self.assertNotIn("## Error", md)

    def test_skipped_lane_no_error_detail_phrase(self):
        """The '(no error detail)' placeholder must not appear for a skipped lane."""
        md = _specialist_md(self._skipped())
        self.assertNotIn("(no error detail)", md)

    def test_skipped_lane_explains_chain_halt(self):
        """The Skipped section body mentions the chain halting at an upstream lane."""
        md = _specialist_md(self._skipped())
        self.assertIn("chain", md.lower())

    def test_skipped_lane_header_rows_present(self):
        """Standard header rows are retained (Provider/Model/OK/Elapsed/Toolset)."""
        lane = AgentResult(
            role="analyst", provider="xai", model="grok",
            ok=False, skipped=True, error=None, elapsed_s=None, toolset=[],
        )
        md = _specialist_md(lane)
        self.assertIn("# Specialist: analyst", md)
        self.assertIn("**Provider:**", md)
        self.assertIn("**Model:**", md)
        self.assertIn("**OK:** False", md)
        self.assertIn("**Elapsed:** n/a", md)

    def test_real_failure_still_renders_error_section(self):
        """Regression: a real failure (ok=False, skipped=False) still renders ## Error."""
        lane = AgentResult(
            role="analyst", provider="xai", model="grok",
            ok=False, skipped=False, error="rate limited", elapsed_s=1.0, toolset=[],
        )
        md = _specialist_md(lane)
        self.assertNotIn("## Skipped", md)
        self.assertIn("## Error", md)
        self.assertIn("rate limited", md)


class TestSequentialConjunctiveCapture(unittest.TestCase):
    """synthesis.md reads the mode-detail, not the aggregate status. A sequential chain
    that broke mid-run but whose convergence step SUCCEEDED must still write the output
    (no data loss); a sequential FAILED run is a first-lane failure, not "all failed"."""

    def test_judge_degraded_with_grade_is_written_not_discarded(self):
        # Data-loss fix: sequential+judge broke mid-run but the judge SUCCEEDED over the
        # survivors → DEGRADED with result.judge set. The grade MUST land in synthesis.md.
        lanes = [
            _lane(role="scout", ok=True, text="found"),
            _lane(role="analyst", ok=False),
            _skipped_lane("writer", "openrouter", "w/m"),
        ]
        result = FleetResult(
            fleet="f", task="t", specialists=lanes,
            convergence="judge", topology="sequential", status=FleetStatus.DEGRADED,
            judge="GRADE TEXT HERE", judge_ok=True, terminal_produced=False,
        )
        md = _synthesis_md(result)
        self.assertIn("# Judge grade", md)
        self.assertIn("GRADE TEXT HERE", md)
        self.assertNotIn("No judge grade", md)

    def test_judge_degraded_without_grade_still_notes_failure(self):
        # Judge genuinely ran and failed (judge None) → the "No judge grade" note holds.
        lanes = [_lane(role="scout", ok=True, text="found"),
                 _lane(role="analyst", ok=True, text="a"),
                 _lane(role="writer", ok=True, text="w")]
        result = FleetResult(
            fleet="f", task="t", specialists=lanes,
            convergence="judge", topology="sequential", status=FleetStatus.DEGRADED,
            judge=None, judge_ok=False, terminal_produced=True,
            notes=["judge failed: timeout"],
        )
        md = _synthesis_md(result)
        self.assertIn("No judge grade", md)
        self.assertIn("timeout", md)

    def test_collect_failed_sequential_says_chain_halted(self):
        lanes = [_lane(role="scout", ok=False),
                 _skipped_lane("analyst", "openrouter", "a/m"),
                 _skipped_lane("writer", "openrouter", "w/m")]
        result = FleetResult(
            fleet="f", task="t", specialists=lanes,
            convergence="collect", topology="sequential", status=FleetStatus.FAILED,
            terminal_produced=False,
        )
        md = _synthesis_md(result)
        self.assertIn("chain halted at the first lane", md)
        self.assertIn("collect mode", md)
        self.assertNotIn("specialists failed", md)

    def test_judge_failed_sequential_says_chain_halted(self):
        lanes = [_lane(role="scout", ok=False),
                 _skipped_lane("analyst", "openrouter", "a/m")]
        result = FleetResult(
            fleet="f", task="t", specialists=lanes,
            convergence="judge", topology="sequential", status=FleetStatus.FAILED,
            judge=None, judge_ok=None, terminal_produced=False,
        )
        md = _synthesis_md(result)
        self.assertIn("chain halted at the first lane", md)
        self.assertIn("judge mode", md)
        self.assertNotIn("specialists failed", md)

    def test_synthesize_failed_sequential_says_chain_halted(self):
        # Sequential+synthesize FAILED = first lane failed, rest skipped — the markdown
        # must mirror the collect/judge wording, NOT a misleading "1 of 3 failed" count.
        lanes = [_lane(role="scout", ok=False),
                 _skipped_lane("analyst", "openrouter", "a/m"),
                 _skipped_lane("writer", "openrouter", "w/m")]
        result = FleetResult(
            fleet="f", task="t", specialists=lanes,
            convergence="synthesize", topology="sequential", status=FleetStatus.FAILED,
            synthesis=None, synth_ok=None, terminal_produced=False,
        )
        md = _synthesis_md(result)
        self.assertIn("chain halted at the first lane", md)
        self.assertIn("synthesis was not attempted", md)
        self.assertNotIn("of 3 specialists failed", md)


# ---------------------------------------------------------------------------
# Iterative topology: round_subdir helper
# ---------------------------------------------------------------------------


class TestRoundSubdir(unittest.TestCase):
    """round_subdir returns the expected string for 1-based round numbers."""

    def test_round_1(self):
        self.assertEqual(round_subdir(1), "round-1")

    def test_round_2(self):
        self.assertEqual(round_subdir(2), "round-2")

    def test_round_10(self):
        self.assertEqual(round_subdir(10), "round-10")


# ---------------------------------------------------------------------------
# Iterative topology: save_lane with subdir (U5)
# ---------------------------------------------------------------------------


class TestSaveLaneSubdir(unittest.TestCase):
    """save_lane(lane, filename, run_dir, subdir=...) writes into run_dir/subdir/filename."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_no_subdir_writes_flat(self):
        """Without subdir, file lands directly in run_dir (regression guard)."""
        lane = _lane("web")
        save_lane(lane, "specialist-web.md", self.run_dir)
        self.assertTrue((self.run_dir / "specialist-web.md").exists())

    def test_subdir_creates_round_dir(self):
        """With subdir='round-1', file lands in run_dir/round-1/."""
        lane = _lane("alpha")
        save_lane(lane, "specialist-alpha.md", self.run_dir, subdir="round-1")
        self.assertTrue((self.run_dir / "round-1" / "specialist-alpha.md").exists())

    def test_subdir_not_at_flat_level(self):
        """With subdir set, the file must NOT exist at the flat run_dir level."""
        lane = _lane("alpha")
        save_lane(lane, "specialist-alpha.md", self.run_dir, subdir="round-1")
        self.assertFalse((self.run_dir / "specialist-alpha.md").exists())

    def test_subdir_round_dir_mode_0o700(self):
        """Round subdirectory is created owner-only (0o700)."""
        lane = _lane("alpha")
        save_lane(lane, "specialist-alpha.md", self.run_dir, subdir="round-1")
        mode = stat.S_IMODE((self.run_dir / "round-1").stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_two_rounds_same_role_no_overwrite(self):
        """Round-1 and round-2 files for the same role coexist — no overwrite."""
        lane1 = _lane("alpha", text="round-one-output")
        lane2 = _lane("alpha", text="round-two-output")
        save_lane(lane1, "specialist-alpha.md", self.run_dir, subdir="round-1")
        save_lane(lane2, "specialist-alpha.md", self.run_dir, subdir="round-2")
        r1 = (self.run_dir / "round-1" / "specialist-alpha.md").read_text()
        r2 = (self.run_dir / "round-2" / "specialist-alpha.md").read_text()
        self.assertIn("round-one-output", r1)
        self.assertIn("round-two-output", r2)
        self.assertNotEqual(r1, r2)

    def test_existing_callers_unaffected_keyword_only(self):
        """subdir is keyword-only: positional calls save_lane(l, f, d) still work."""
        lane = _lane("web")
        # Must not raise — existing positional signature unchanged
        save_lane(lane, "specialist-web.md", self.run_dir)
        self.assertTrue((self.run_dir / "specialist-web.md").exists())


# ---------------------------------------------------------------------------
# Iterative topology: _build_manifest rounds + diversity_collapsed fields (U5)
# ---------------------------------------------------------------------------


def _iterative_lane(role, *, round_num=1, ok=True, provider="openrouter", model="a/m",
                    toolset=None) -> AgentResult:
    """AgentResult for an iterative round lane — toolset defaults to [] (not None)."""
    if toolset is None:
        toolset = []
    text = f"{role}-r{round_num}-output" if ok else None
    error = None if ok else f"{role}-r{round_num}-error"
    return AgentResult(
        role=role, provider=provider, model=model, ok=ok,
        text=text, error=error, elapsed_s=0.5, toolset=toolset,
    )


def _iterative_cfg_for_manifest(**overrides):
    """2-lane, 2-round iterative+collect config for manifest-only tests."""
    data = {
        "name": "test-iterate",
        "topology": "iterative",
        "rounds": 2,
        "convergence": "collect",
        "specialists": [
            {"role": "alpha", "provider": "openrouter", "model": "a/m",
             "toolset": [], "focus": "analyze"},
            {"role": "beta", "provider": "xai", "model": "b/m",
             "toolset": [], "focus": "verify"},
        ],
    }
    data.update(overrides)
    return FleetConfig.from_dict(data)


def _iterative_result_for_manifest(*, rounds=None, diversity_collapsed=False,
                                   status=FleetStatus.SUCCESS) -> FleetResult:
    """FleetResult for a 2-lane, 2-round iterative+collect run.

    ``rounds`` defaults to a 2×2 transcript (all ok).  ``specialists`` is the
    last-surviving-round representatives (round-2 results).
    """
    if rounds is None:
        round1 = [
            _iterative_lane("alpha", round_num=1),
            _iterative_lane("beta", round_num=1),
        ]
        round2 = [
            _iterative_lane("alpha", round_num=2),
            _iterative_lane("beta", round_num=2),
        ]
        rounds = [round1, round2]

    # specialists = representatives from the last surviving round (ok lanes)
    specialists = [lane for lane in rounds[-1] if lane.ok]

    return FleetResult(
        fleet="test-iterate",
        task="test task",
        specialists=specialists,
        synthesis=None,
        synth_ok=None,
        convergence="collect",
        topology="iterative",
        status=status,
        rounds=rounds,
        diversity_collapsed=diversity_collapsed,
    )


class TestIterativeManifest(unittest.TestCase):
    """_build_manifest (via save_run) emits correct rounds[] and diversity_collapsed
    for iterative topology.  Non-iterative regression guards in the class below."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _load_manifest(self, cfg=None, result=None):
        if cfg is None:
            cfg = _iterative_cfg_for_manifest()
        if result is None:
            result = _iterative_result_for_manifest()
        save_run(cfg, result, self.run_dir)
        return json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))

    def test_rounds_field_is_list(self):
        """Iterative manifest has a non-null 'rounds' array."""
        manifest = self._load_manifest()
        self.assertIsInstance(manifest["rounds"], list)

    def test_rounds_length_matches_result(self):
        """rounds[] length equals len(result.rounds)."""
        manifest = self._load_manifest()
        self.assertEqual(len(manifest["rounds"]), 2)

    def test_rounds_per_round_lane_count(self):
        """Each round entry has one record per specialist in that round."""
        manifest = self._load_manifest()
        for k, round_entry in enumerate(manifest["rounds"]):
            self.assertEqual(
                len(round_entry), 2, f"round {k + 1} should have 2 lane records"
            )

    def test_rounds_file_uses_round_subdir_scheme(self):
        """rounds[k][i].file follows round-<k+1>/<filename> scheme."""
        manifest = self._load_manifest()
        fmap = lane_filename_map(["alpha", "beta"])
        for k, round_entry in enumerate(manifest["rounds"]):
            expected_subdir = round_subdir(k + 1)
            for record in round_entry:
                expected_file = f"{expected_subdir}/{fmap[record['role']]}"
                self.assertEqual(
                    record["file"], expected_file,
                    f"round {k + 1} role={record['role']!r}: wrong file path",
                )

    def test_rounds_toolset_is_list_never_none(self):
        """All rounds[k][i].toolset values are lists — []-vs-None invariant."""
        manifest = self._load_manifest()
        for k, round_entry in enumerate(manifest["rounds"]):
            for record in round_entry:
                self.assertIsInstance(
                    record["toolset"], list,
                    f"round {k + 1} role={record['role']!r}: toolset must be list not None",
                )

    def test_rounds_required_fields_present(self):
        """Each round lane record carries all required fields."""
        required = {
            "role", "provider", "model", "ok", "error",
            "elapsed_s", "toolset", "timed_out", "file",
        }
        manifest = self._load_manifest()
        for k, round_entry in enumerate(manifest["rounds"]):
            for record in round_entry:
                missing = required - record.keys()
                self.assertFalse(
                    missing, f"round {k + 1} role={record['role']!r} missing {missing}"
                )

    def test_diversity_collapsed_false_by_default(self):
        """diversity_collapsed=False in manifest when debate ran normally."""
        manifest = self._load_manifest(result=_iterative_result_for_manifest(diversity_collapsed=False))
        self.assertIs(manifest["diversity_collapsed"], False)

    def test_diversity_collapsed_true_when_set(self):
        """diversity_collapsed=True propagated to manifest when debate collapsed."""
        manifest = self._load_manifest(result=_iterative_result_for_manifest(diversity_collapsed=True))
        self.assertIs(manifest["diversity_collapsed"], True)

    def test_top_level_lanes_are_representatives(self):
        """Top-level lanes[] lists result.specialists (last-round reps), not all rounds."""
        manifest = self._load_manifest()
        # 2-lane iterative with 2-round all-ok → representatives are 2 round-2 lanes
        self.assertEqual(len(manifest["lanes"]), 2)
        roles_in_lanes = {lane["role"] for lane in manifest["lanes"]}
        self.assertEqual(roles_in_lanes, {"alpha", "beta"})

    def test_topology_is_iterative(self):
        """Manifest carries topology='iterative'."""
        manifest = self._load_manifest()
        self.assertEqual(manifest["topology"], "iterative")


class TestNonIterativeManifestAfterIterativeAdds(unittest.TestCase):
    """Non-iterative (parallel, sequential) manifests emit rounds=None and
    diversity_collapsed=False — additive; no regression on existing fields."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _load_parallel(self):
        save_run(_cfg(), _result(), self.run_dir)
        return json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))

    def _chain_result(self):
        return FleetResult(
            fleet="test-chain", task="t",
            specialists=[
                _lane("scout", provider="openrouter", model="s/m", toolset=["web"]),
                _lane("analyst", provider="xai", model="grok", toolset=["web"]),
                _lane("writer", provider="openrouter", model="w/m", toolset=[]),
            ],
            synthesis=None, synth_ok=None, convergence="collect",
            topology="sequential", terminal_produced=True,
            threading_truncated=False, status=FleetStatus.SUCCESS,
        )

    def test_parallel_rounds_null(self):
        """Parallel manifest: rounds=null."""
        self.assertIsNone(self._load_parallel()["rounds"])

    def test_parallel_diversity_collapsed_false(self):
        """Parallel manifest: diversity_collapsed=False."""
        self.assertIs(self._load_parallel()["diversity_collapsed"], False)

    def test_parallel_existing_topology_field_unchanged(self):
        """Parallel manifest: topology='parallel' unchanged."""
        self.assertEqual(self._load_parallel()["topology"], "parallel")

    def test_parallel_existing_status_unchanged(self):
        """Parallel manifest: status field unchanged."""
        self.assertEqual(self._load_parallel()["status"], "success")

    def test_sequential_rounds_null(self):
        """Sequential manifest: rounds=null."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            save_run(_chain_cfg(), self._chain_result(), run_dir)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertIsNone(manifest["rounds"])

    def test_sequential_diversity_collapsed_false(self):
        """Sequential manifest: diversity_collapsed=False."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            save_run(_chain_cfg(), self._chain_result(), run_dir)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertIs(manifest["diversity_collapsed"], False)


# ---------------------------------------------------------------------------
# Iterative topology: incremental capture via run_with_progress (U5 + U6 edge)
# ---------------------------------------------------------------------------


def _iterative_run_cfg(**overrides):
    """2-lane, 2-round iterative+collect config for progress_runner tests.

    Uses resolve() so effective_instruction is set (required by run_fleet).
    """
    data = {
        "name": "test-iterate",
        "topology": "iterative",
        "rounds": 2,
        "convergence": "collect",
        "specialists": [
            {"role": "alpha", "provider": "openrouter", "model": "a/m",
             "toolset": [], "focus": "analyze"},
            {"role": "beta", "provider": "xai", "model": "b/m",
             "toolset": [], "focus": "verify"},
        ],
    }
    data.update(overrides)
    cfg = FleetConfig.from_dict(data)
    resolve(cfg, "/unused")
    return cfg


class TestIterativeIncrementalCapture(unittest.TestCase):
    """run_with_progress writes round-N/specialist-<role>.md for iterative fleets.

    Core fix: without round namespacing, LaneDone fires once per lane per round
    and the same filename is overwritten each round.  This class proves both the
    absence of overwriting AND the coupling contract (manifest paths == on-disk
    paths).
    """

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def _run_and_save(self, cfg, client):
        """Run run_with_progress + save_run, returning (result, manifest_dict)."""
        import io
        result = run_with_progress(
            cfg, "test task", client,
            run_dir=self.run_dir,
            progress_stream=io.StringIO(),
        )
        save_run(cfg, result, self.run_dir)
        manifest = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        return result, manifest

    def test_lanes_file_points_at_representative_round(self):
        """Top-level lanes[].file points at each representative's ACTUAL round file —
        the drop-round for a dropped lane, R* for a survivor — and every path exists on
        disk. Guards the cross-unit bug where lanes[].file named a flat
        specialist-<role>.md that the iterative edge never writes."""
        cfg = _iterative_config(convergence="collect")  # alpha, beta, gamma; 3 rounds
        # alpha fails in round 2 → drops; beta and gamma survive to round 3 (R*).
        client = RoundAwareFakeClient(schedule={("alpha", 2): ("fail", "alpha dropped")})
        _, manifest = self._run_and_save(cfg, client)
        files = {lane["role"]: lane["file"] for lane in manifest["lanes"]}
        self.assertEqual(files["alpha"], "round-2/specialist-alpha.md")   # drop round
        self.assertEqual(files["beta"], "round-3/specialist-beta.md")     # R*
        self.assertEqual(files["gamma"], "round-3/specialist-gamma.md")   # R*
        for lane in manifest["lanes"]:
            self.assertTrue(
                (self.run_dir / lane["file"]).exists(),
                f"lanes[].file {lane['file']!r} must exist on disk (not a phantom flat path)",
            )

    def test_breadcrumb_names_round_namespaced_path(self):
        """The `[cadre] lane … -> <file>` breadcrumb names the round-namespaced path
        (round-k/…), matching where save_lane actually writes — the "-> <file> is on
        disk" contract must hold for iterative, not point at a flat path."""
        import io
        import re
        cfg = _iterative_run_cfg()
        client = RoundAwareFakeClient()
        stream = io.StringIO()
        run_with_progress(
            cfg, "task", client, run_dir=self.run_dir, progress_stream=stream,
        )
        lane_files = re.findall(r"-> (\S+\.md)", stream.getvalue())
        self.assertTrue(lane_files, "no lane breadcrumbs with '-> <file>' were emitted")
        for f in lane_files:
            self.assertTrue(
                f.startswith("round-"),
                f"breadcrumb path {f!r} should be round-namespaced (round-k/…)",
            )
            self.assertTrue(
                (self.run_dir / f).exists(), f"breadcrumb path {f!r} must exist on disk",
            )

    def test_round_1_dir_created(self):
        """After run, round-1/ subdirectory exists under run_dir."""
        cfg = _iterative_run_cfg()
        client = RoundAwareFakeClient()
        self._run_and_save(cfg, client)
        self.assertTrue((self.run_dir / "round-1").is_dir())

    def test_round_2_dir_created(self):
        """After run, round-2/ subdirectory exists under run_dir."""
        cfg = _iterative_run_cfg()
        client = RoundAwareFakeClient()
        self._run_and_save(cfg, client)
        self.assertTrue((self.run_dir / "round-2").is_dir())

    def test_both_rounds_have_alpha_file(self):
        """specialist-alpha.md exists in both round-1/ and round-2/ (no overwrite)."""
        cfg = _iterative_run_cfg()
        client = RoundAwareFakeClient()
        self._run_and_save(cfg, client)
        self.assertTrue((self.run_dir / "round-1" / "specialist-alpha.md").exists())
        self.assertTrue((self.run_dir / "round-2" / "specialist-alpha.md").exists())

    def test_both_rounds_have_beta_file(self):
        """specialist-beta.md exists in both round-1/ and round-2/ (no overwrite)."""
        cfg = _iterative_run_cfg()
        client = RoundAwareFakeClient()
        self._run_and_save(cfg, client)
        self.assertTrue((self.run_dir / "round-1" / "specialist-beta.md").exists())
        self.assertTrue((self.run_dir / "round-2" / "specialist-beta.md").exists())

    def test_round_files_have_distinct_content(self):
        """Round-1 and round-2 files for the same role differ in content.

        RoundAwareFakeClient returns 'alpha-r1-output' and 'alpha-r2-output' for
        consecutive calls, so the files must contain different text if (and only if)
        round namespacing prevented overwrite.
        """
        cfg = _iterative_run_cfg()
        client = RoundAwareFakeClient()
        self._run_and_save(cfg, client)
        r1 = (self.run_dir / "round-1" / "specialist-alpha.md").read_text()
        r2 = (self.run_dir / "round-2" / "specialist-alpha.md").read_text()
        self.assertIn("alpha-r1-output", r1, "round-1 file must contain r1 output")
        self.assertIn("alpha-r2-output", r2, "round-2 file must contain r2 output")
        self.assertNotEqual(r1, r2)

    def test_no_flat_specialist_files_written(self):
        """For iterative, no specialist-*.md files land at the flat run_dir level.

        All per-lane files must be under round-N/ subdirs — a flat file would mean
        the topology gate in the hook was skipped.
        """
        cfg = _iterative_run_cfg()
        client = RoundAwareFakeClient()
        self._run_and_save(cfg, client)
        flat_files = list(self.run_dir.glob("specialist-*.md"))
        self.assertEqual(
            flat_files, [],
            f"Iterative run wrote flat specialist files (should be under round-N/): {flat_files}",
        )

    def test_coupling_manifest_round_files_exist_on_disk(self):
        """Every manifest rounds[k][i].file that is ok=True exists on disk.

        This is the load-bearing coupling test: round_subdir is shared by the
        progress hook (save_lane) and _build_rounds_manifest, so the manifest's
        named paths must exactly match what is actually on disk.
        """
        cfg = _iterative_run_cfg()
        client = RoundAwareFakeClient()
        _, manifest = self._run_and_save(cfg, client)
        for k, round_entry in enumerate(manifest["rounds"]):
            for record in round_entry:
                if record["ok"]:
                    file_path = self.run_dir / record["file"]
                    self.assertTrue(
                        file_path.exists(),
                        f"round {k + 1} role={record['role']!r}: "
                        f"manifest names {record['file']!r} but it is not on disk",
                    )


# ---------------------------------------------------------------------------
# Iterative topology: _synthesis_md falls through to parallel wording (U5)
# ---------------------------------------------------------------------------


class TestIterativeSynthesisMd(unittest.TestCase):
    """_synthesis_md for iterative topology uses parallel-style wording.

    The topology-specific branches in _synthesis_md only check for 'sequential'.
    'iterative' is a different string, so it must fall through to the same wording
    as parallel.  These tests guard the contract so a future 'iterative' branch
    cannot accidentally introduce sequential-style wording.
    """

    def _result_with(self, **kwargs):
        """Build a minimal FleetResult with topology='iterative'."""
        defaults = dict(
            fleet="f", task="t", topology="iterative",
            specialists=[_lane("alpha"), _lane("beta")],
            status=FleetStatus.SUCCESS,
        )
        defaults.update(kwargs)
        return FleetResult(**defaults)

    def test_collect_success_uses_attributed_blocks(self):
        """Iterative+collect SUCCESS: synthesis.md contains attributed specialist blocks."""
        result = self._result_with(
            convergence="collect",
            specialists=[_lane("alpha", text="alpha out"), _lane("beta", text="beta out")],
            synthesis=None, synth_ok=None,
        )
        md = _synthesis_md(result)
        self.assertIn("alpha out", md)
        self.assertIn("beta out", md)
        self.assertIn("# Collected specialist outputs", md)

    def test_collect_failed_uses_parallel_wording(self):
        """Iterative+collect FAILED: 'all N specialists failed' — NOT 'chain halted'."""
        result = self._result_with(
            convergence="collect",
            specialists=[_lane("alpha", ok=False), _lane("beta", ok=False)],
            synthesis=None, synth_ok=None,
            status=FleetStatus.FAILED,
        )
        md = _synthesis_md(result)
        self.assertIn("specialists failed", md)
        self.assertNotIn("chain halted", md)

    def test_synthesize_failed_uses_parallel_wording(self):
        """Iterative+synthesize FAILED: 'N of N failed; not attempted' — NOT 'chain halted'."""
        result = self._result_with(
            convergence="synthesize",
            specialists=[_lane("alpha", ok=False), _lane("beta", ok=False)],
            synthesis=None, synth_ok=None,
            status=FleetStatus.FAILED,
        )
        md = _synthesis_md(result)
        self.assertIn("synthesis was not attempted", md)
        self.assertNotIn("chain halted", md)

    def test_synthesize_success_returns_synthesis_text(self):
        """Iterative+synthesize SUCCESS: the synthesis text is returned verbatim."""
        result = self._result_with(
            convergence="synthesize",
            specialists=[_lane("alpha"), _lane("beta")],
            synthesis="# Synthesis\n\nResult here.",
            synth_ok=True,
        )
        md = _synthesis_md(result)
        self.assertIn("# Synthesis", md)
        self.assertIn("Result here.", md)

    def test_judge_failed_uses_parallel_wording(self):
        """Iterative+judge FAILED (no survivors): 'all N failed' — NOT 'chain halted'."""
        result = self._result_with(
            convergence="judge",
            specialists=[_lane("alpha", ok=False), _lane("beta", ok=False)],
            synthesis=None, synth_ok=None,
            judge=None, judge_ok=None,
            status=FleetStatus.FAILED,
        )
        md = _synthesis_md(result)
        self.assertIn("specialists failed", md)
        self.assertIn("judge mode", md)
        self.assertNotIn("chain halted", md)


class TestCaptureSinkCompleteness(unittest.TestCase):
    """Codex #5 findings: the failure-note capture path and the fleet-controlled
    manifest identity fields must be sanitized like every other capture sink."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_synth_failure_note_sanitized_in_synthesis_md(self):
        result = _result(
            synthesis=None, ok=False, synth_ok=False,
            notes=["synthesizer failed: boom \x1b[2K forged"],
        )
        md = _synthesis_md(result)
        self.assertNotIn("\x1b", md)
        self.assertIn("boom", md)

    def test_judge_failure_note_sanitized_in_synthesis_md(self):
        result = _judge_result(
            judge=None, judge_ok=False, ok=False,
            notes=["judge failed: boom \x1b[31m forged"],
        )
        md = _synthesis_md(result)
        self.assertNotIn("\x1b", md)
        self.assertIn("boom", md)

    def test_manifest_identity_fields_sanitized(self):
        # A tampered fleet's provider/model carry an escape byte; the manifest a
        # consumer prints must not smuggle it (matches the .md blocks + render).
        lane = _lane(role="web", provider="acme\x1b[2K", model="m\x1b[31m", toolset=["web"])
        result = _result(specialists=[lane], synthesis="# T\n\nbody", ok=True, synth_ok=True)
        save_run(_cfg(), result, self.run_dir)
        manifest = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("\x1b", manifest["lanes"][0]["provider"])
        self.assertNotIn("\x1b", manifest["lanes"][0]["model"])
        self.assertNotIn("\x1b", json.dumps(manifest["models"]))

    def test_manifest_toolset_entries_sanitized(self):
        lane = _lane(role="web", toolset=["web\x1b[2K", "x_search"])
        result = _result(specialists=[lane], synthesis="# T\n\nbody", ok=True, synth_ok=True)
        save_run(_cfg(), result, self.run_dir)
        manifest = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("\x1b", json.dumps(manifest["lanes"][0]["toolset"]))
        self.assertEqual(manifest["lanes"][0]["toolset"], ["web[2K", "x_search"])  # [] stays list

    def test_manifest_grade_entry_identity_sanitized(self):
        # grade-entry role/model (from the matched lane) are sanitized like lanes[].
        lane = _lane(role="web", model="m\x1b[31m", toolset=["web"])
        judge_text = f"=== LANE: web {_JUDGE_NONCE} ===\nGrade: A\nRationale: ok.\n"
        result = _judge_result(judge=judge_text, judge_ok=True, specialists=[lane])
        save_run(_judge_cfg(), result, self.run_dir)
        manifest = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["grades"])  # the lane graded
        self.assertNotIn("\x1b", json.dumps(manifest["grades"]))


if __name__ == "__main__":
    unittest.main()
