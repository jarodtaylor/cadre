"""Tests for fleet_engine.capture.save_run (U2).

All tests inject a tempfile.mkdtemp() run_dir and never touch ~/.cadre.
Fixtures build FleetResult/AgentResult directly — no live model calls.
"""

import json
import os
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
    save_lane,
    save_prompt,
    save_run,
)
from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult, run_fleet
from fleet_engine.model_client import AgentResult
from fleet_engine.personas import resolve
from fleet_engine.render import render_result


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
        ok=ok,
        synth_ok=synth_ok,
        notes=notes or [],
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
    """manifest records HERMES_HOME when set, and the default when unset."""

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

    def test_hermes_home_default_when_unset(self):
        env_without_hermes = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        with patch.dict(os.environ, env_without_hermes, clear=True):
            save_run(_cfg(), _result(), self.run_dir)
        manifest = self._load_manifest()
        self.assertEqual(manifest["hermes_home"], "~/.hermes")


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
        ok=ok,
        synth_ok=None,
        notes=[],
        convergence="collect",
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
            synthesis=None, ok=False, synth_ok=None, convergence="collect",
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
            synthesis=None, ok=False, synth_ok=None, convergence="collect",
        )
        content = self._synthesis_content(result)
        self.assertIn("2", content)


class TestOnDiskIdentitySanitization(unittest.TestCase):
    """On-disk markdown must not let newlines in identity fields forge structure (finding #4).

    role/provider/model are attacker-controllable (fleet tampering) and are used as
    markdown delimiters in persisted run records, which vouch for attribution. A
    newline could forge a second header or '--- role ---' block. They are _sanitize'd
    exactly as the terminal renderer does; lane.text (content) stays raw.
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

    def test_collect_synthesis_md_body_text_preserved_raw(self):
        # CONTENT (lane.text) is NOT an identity claim — it stays raw, multi-line intact.
        lane = _lane(role="web", toolset=["web"], text="line one\nline two", ok=True)
        md = _synthesis_md(_collect_result(specialists=[lane]))
        self.assertIn("line one\nline two", md)


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
            synthesis=None, ok=False, synth_ok=None,
            convergence="synthesize",
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

# Canonical two-lane judge text that parse_grades can fully parse.
_JUDGE_TEXT_FULL = (
    "=== LANE: web ===\nGrade: A\nRationale: Excellent grounding.\n\n"
    "=== LANE: social ===\nGrade: B\nRationale: Good social coverage."
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
        ok=ok,
        synth_ok=None,
        notes=notes or [],
        convergence="judge",
        judge=judge,
        judge_ok=judge_ok,
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
        partial_judge_text = "=== LANE: web ===\nGrade: A\nRationale: Good."
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


class TestJudgeGradeUnmutated(unittest.TestCase):
    """The judge grade text must reach disk UNCHANGED — _sanitize is the render boundary's job.

    KTD8: capture writes the judge text verbatim; the terminal renderer (_sanitize) is
    the only place that strips control characters. A terminal escape in the judge text
    must survive in synthesis.md — if capture sanitized it, that would corrupt the record.
    """

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_terminal_escape_survives_in_synthesis_md(self):
        """A terminal escape in the judge text is preserved verbatim on disk (KTD8)."""
        # Use a clearly-synthetic ANSI escape as a canary — _sanitize would strip it.
        escape = "\x1b[31m"
        judge_text_with_escape = (
            f"{escape}=== LANE: web ===\nGrade: A\nRationale: {escape}Bold finding."
        )
        result = _judge_result(judge=judge_text_with_escape)
        save_run(_judge_cfg(), result, self.run_dir)
        on_disk = (self.run_dir / "synthesis.md").read_text(encoding="utf-8")
        self.assertIn(escape, on_disk,
                      "KTD8 violated: capture _sanitize'd the judge text; it must stay raw")


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

    # Canonical judge text: both lanes graded → parse_grades returns 2 entries, 0 ungraded.
    _JUDGE_RESPONSE = (
        "=== LANE: web ===\nGrade: A\nRationale: Strong grounding.\n\n"
        "=== LANE: social ===\nGrade: B\nRationale: Adequate social coverage."
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
        self.assertEqual(result.judge, self._JUDGE_RESPONSE)
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


if __name__ == "__main__":
    unittest.main()
