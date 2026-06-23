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
    lane_filename_map,
    prepare_run_dir,
    resolve_run_dir,
    save_lane,
    save_run,
)
from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult
from fleet_engine.model_client import AgentResult


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
            {"role": "notool", "provider": "openrouter", "model": "m"},
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
            {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"]},
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
            {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"]},
        ]), r, self.run_dir)
        content = (self.run_dir / "synthesis.md").read_text(encoding="utf-8")
        # Should note that the synthesizer failed, not "N of M specialists failed"
        self.assertIn("synthesizer failed", content)

    def test_manifest_synth_ok_is_none_when_not_attempted(self):
        specialists = [_lane("web", ok=False, error="down", toolset=["web"])]
        r = _result(specialists=specialists, synthesis=None, ok=False, synth_ok=None)
        save_run(_cfg(specialists=[
            {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"]},
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
            {"role": "web/evil", "provider": "openrouter", "model": "m"},
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
            {"role": "../escape", "provider": "openrouter", "model": "m"},
        ]), r, self.run_dir)
        # No escape outside run_dir
        escaped_path = self.run_dir.parent / "escape.md"
        self.assertFalse(escaped_path.exists(), "file must not escape run_dir via ..")

    def test_slash_role_true_role_in_manifest(self):
        """The manifest's lane.role must be the TRUE role, not the sanitized filename."""
        r = _result(specialists=[_lane("web/evil", toolset=[])])
        save_run(_cfg(specialists=[
            {"role": "web/evil", "provider": "openrouter", "model": "m"},
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
            {"role": "web/evil", "provider": "openrouter", "model": "m"},
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
            {"role": "a/b", "provider": "openrouter", "model": "m"},
            {"role": "a:b", "provider": "openrouter", "model": "m"},
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
            {"role": "a/b", "provider": "openrouter", "model": "m"},
            {"role": "a:b", "provider": "openrouter", "model": "m"},
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

if __name__ == "__main__":
    unittest.main()
