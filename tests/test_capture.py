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

from fleet_engine.capture import save_run
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
    """save_run writes prompt.txt + one specialist file per specialist + synthesis.md + manifest.json."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)

    def test_prompt_txt_written(self):
        r = _result()
        save_run(_cfg(), r, self.run_dir)
        prompt_path = self.run_dir / "prompt.txt"
        self.assertTrue(prompt_path.exists())
        self.assertEqual(prompt_path.read_text(encoding="utf-8"), r.task)

    def test_one_specialist_file_per_specialist(self):
        r = _result()
        save_run(_cfg(), r, self.run_dir)
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
        lane = next(l for l in self.manifest["lanes"] if l["role"] == "web")
        self.assertIn("role", lane)
        self.assertIn("provider", lane)
        self.assertIn("model", lane)
        self.assertIn("ok", lane)
        self.assertIn("error", lane)
        self.assertIn("elapsed_s", lane)
        self.assertIn("toolset", lane)
        self.assertIn("timed_out", lane)

    def test_lane_elapsed_s_value(self):
        lane = next(l for l in self.manifest["lanes"] if l["role"] == "web")
        self.assertAlmostEqual(lane["elapsed_s"], 2.5, places=5)

    def test_lane_toolset_list(self):
        lane = next(l for l in self.manifest["lanes"] if l["role"] == "web")
        self.assertEqual(lane["toolset"], ["web"])

    def test_synthesizer_not_a_lane_entry(self):
        """Synthesizer appears at run-level only (synth_ok), not in lanes."""
        roles = [l["role"] for l in self.manifest["lanes"]]
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
        failed = _lane("social", ok=False, error="auth error", toolset=["x_search"])
        r = _result(specialists=[_lane("web", toolset=["web"]), failed])
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
        lane = next(l for l in manifest["lanes"] if l["role"] == "social")
        self.assertFalse(lane["ok"])
        self.assertEqual(lane["error"], "auth error")

    def test_folder_still_complete_with_failed_specialist(self):
        failed = _lane("social", ok=False, error="auth error", toolset=["x_search"])
        r = _result(specialists=[_lane("web", toolset=["web"]), failed])
        save_run(_cfg(), r, self.run_dir)
        for fname in ("prompt.txt", "specialist-web.md", "specialist-social.md",
                      "synthesis.md", "manifest.json"):
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
        lane = next(l for l in manifest["lanes"] if l["role"] == "social")
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
        specialists = [
            _lane("web", ok=False, error="down", toolset=["web"]),
            _lane("social", ok=False, error="down", provider="xai",
                  model="grok-4.3", toolset=["x_search"]),
        ]
        r = _result(specialists=specialists, synthesis=None, ok=False, synth_ok=None)
        save_run(_cfg(), r, self.run_dir)
        for fname in ("prompt.txt", "specialist-web.md", "specialist-social.md",
                      "synthesis.md", "manifest.json"):
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

    def test_prompt_txt_is_0o600(self):
        save_run(_cfg(), _result(), self.run_dir)
        mode = stat.S_IMODE((self.run_dir / "prompt.txt").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_specialist_files_are_0o600(self):
        save_run(_cfg(), _result(), self.run_dir)
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
        save_run(_cfg(), _result(), self.run_dir)
        artifacts = list(self.run_dir.iterdir())
        names = {p.name for p in artifacts}
        self.assertIn("prompt.txt", names)
        self.assertIn("manifest.json", names)
        self.assertIn("synthesis.md", names)
        # All artifacts are immediate children of run_dir (or subdirs thereof)
        for p in artifacts:
            self.assertTrue(str(p).startswith(str(self.run_dir)))

    def test_save_run_creates_run_dir_if_missing(self):
        nested = self.run_dir / "nested" / "leaf"
        self.assertFalse(nested.exists())
        save_run(_cfg(), _result(), nested)
        self.assertTrue(nested.exists())
        self.assertTrue((nested / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
