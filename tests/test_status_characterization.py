"""
Characterization suite — frozen baseline (U1 of result-status-enum refactor).

For every (convergence-mode × outcome) pair this file:
  - runs the REAL run_fleet over FakeClient (no live models, no I/O)
  - asserts render_result() output via assertIn on exact header lines and key markers
  - asserts the save_run() manifest JSON on deterministic status-bearing keys
  - asserts result.ok (the exit-code input)

These tests MUST stay green through U2–U5 to prove byte-identical behaviour
preservation.  They freeze today's observable behaviour as the oracle; the later
refactor units re-run this file unchanged.

Do NOT modify production code in this unit.

Dynamic fields excluded from the golden:
  - manifest["timestamp"]  — datetime.now(), asserted present + non-empty string only
  - manifest["lanes"][].elapsed_s — wall-clock float; FakeClient returns None, but
    asserted as float-or-None to stay correct under any client.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fleet_engine.capture import save_run
from fleet_engine.engine import run_fleet
from fleet_engine.render import render_result
from tests.test_engine import (
    FakeClient,
    _collect_config,
    _config,
    _judge_config,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _run(cfg, behavior):
    """Run the real run_fleet over FakeClient; return (result, rendered, manifest)."""
    client = FakeClient(behavior)
    result = run_fleet(cfg, "characterization task", client)
    rendered = render_result(result)
    run_dir = Path(tempfile.mkdtemp())
    try:
        save_run(cfg, result, run_dir)
        with open(run_dir / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
    finally:
        shutil.rmtree(run_dir)
    return result, rendered, manifest


def _assert_timing(tc, manifest):
    """Assert non-deterministic timing fields are present and correctly typed."""
    tc.assertIn("timestamp", manifest)
    tc.assertIsInstance(manifest["timestamp"], str)
    tc.assertGreater(len(manifest["timestamp"]), 0)
    for lane in manifest["lanes"]:
        tc.assertIn("elapsed_s", lane)
        tc.assertTrue(
            lane["elapsed_s"] is None or isinstance(lane["elapsed_s"], float),
            f"lanes[].elapsed_s must be None or float, got {lane['elapsed_s']!r}",
        )


# ---------------------------------------------------------------------------
# Scenario 1 — synthesize SUCCESS (all ok)
# ---------------------------------------------------------------------------

class TestSynthesizeSuccess(unittest.TestCase):
    """synthesize mode — all specialists ok, synthesizer ok → SUCCESS."""

    def setUp(self):
        self.result, self.rendered, self.manifest = _run(
            _config(),
            {"synthesizer": ("ok", "SYNTH OUTPUT")},
        )

    def test_ok_is_true(self):
        self.assertTrue(self.result.ok)

    def test_render_header(self):
        self.assertIn("=== t — synthesized result ===", self.rendered)

    def test_render_no_partial_header(self):
        # SUCCESS must not emit the degraded/failed header
        self.assertNotIn("partial result (no synthesis)", self.rendered)

    def test_manifest_convergence(self):
        self.assertEqual(self.manifest["convergence"], "synthesize")

    def test_manifest_synth_ok_true(self):
        self.assertIs(self.manifest["synth_ok"], True)

    def test_manifest_judge_ok_none(self):
        self.assertIsNone(self.manifest["judge_ok"])

    def test_manifest_lanes_all_ok(self):
        self.assertEqual(len(self.manifest["lanes"]), 3)
        for lane in self.manifest["lanes"]:
            self.assertTrue(lane["ok"])

    def test_manifest_timing(self):
        _assert_timing(self, self.manifest)


# ---------------------------------------------------------------------------
# Scenario 2 — synthesize DEGRADED (synthesizer fails, specialists ok)
# ---------------------------------------------------------------------------

class TestSynthesizeDegraded(unittest.TestCase):
    """synthesize mode — synthesizer fails, specialists ok → DEGRADED."""

    def setUp(self):
        self.result, self.rendered, self.manifest = _run(
            _config(),
            {"synthesizer": ("fail", "synth-error")},
        )

    def test_ok_is_false(self):
        self.assertFalse(self.result.ok)

    def test_render_header(self):
        self.assertIn("=== t — partial result (no synthesis) ===", self.rendered)

    def test_render_no_preamble(self):
        # DEGRADED = synthesizer ran + failed; the "synthesis was not attempted"
        # preamble belongs ONLY to FAILED (all specialists failed). Absence here
        # is the load-bearing distinction this suite must freeze.
        self.assertNotIn("synthesis was not attempted", self.rendered)

    def test_manifest_convergence(self):
        self.assertEqual(self.manifest["convergence"], "synthesize")

    def test_manifest_synth_ok_false(self):
        self.assertIs(self.manifest["synth_ok"], False)

    def test_manifest_judge_ok_none(self):
        self.assertIsNone(self.manifest["judge_ok"])

    def test_manifest_lanes_all_ok(self):
        # Specialists all succeeded; degradation was in the synthesizer only.
        for lane in self.manifest["lanes"]:
            self.assertTrue(lane["ok"])

    def test_manifest_timing(self):
        _assert_timing(self, self.manifest)


# ---------------------------------------------------------------------------
# Scenario 3 — synthesize FAILED (all specialists fail)
# ---------------------------------------------------------------------------

class TestSynthesizeFailed(unittest.TestCase):
    """synthesize mode — all specialists fail → FAILED."""

    def setUp(self):
        self.result, self.rendered, self.manifest = _run(
            _config(),
            {"web": ("fail", "boom"), "social": ("fail", "boom"), "analysis": ("fail", "boom")},
        )

    def test_ok_is_false(self):
        self.assertFalse(self.result.ok)

    def test_render_header(self):
        self.assertIn("=== t — partial result (no synthesis) ===", self.rendered)

    def test_render_preamble_present(self):
        # All-fail synthesize run emits the "synthesis was not attempted" preamble.
        self.assertIn("synthesis was not attempted", self.rendered)

    def test_manifest_convergence(self):
        self.assertEqual(self.manifest["convergence"], "synthesize")

    def test_manifest_synth_ok_none(self):
        # synth_ok is None → synthesis was never attempted (distinct from False =
        # synthesizer ran and failed).
        self.assertIsNone(self.manifest["synth_ok"])

    def test_manifest_judge_ok_none(self):
        self.assertIsNone(self.manifest["judge_ok"])

    def test_manifest_lanes_all_failed(self):
        self.assertEqual(len(self.manifest["lanes"]), 3)
        for lane in self.manifest["lanes"]:
            self.assertFalse(lane["ok"])

    def test_manifest_timing(self):
        _assert_timing(self, self.manifest)


# ---------------------------------------------------------------------------
# Scenario 4 — collect SUCCESS (all specialists ok)
# ---------------------------------------------------------------------------

class TestCollectSuccess(unittest.TestCase):
    """collect mode — all specialists ok → SUCCESS."""

    def setUp(self):
        # Empty behavior → FakeClient defaults every role to ("ok", "{role}-output")
        self.result, self.rendered, self.manifest = _run(
            _collect_config(),
            {},
        )

    def test_ok_is_true(self):
        self.assertTrue(self.result.ok)

    def test_render_header(self):
        # Exact success header — does NOT end with "all specialists failed"
        self.assertIn("=== t — collect result ===", self.rendered)

    def test_render_not_all_failed(self):
        self.assertNotIn("all specialists failed", self.rendered)

    def test_manifest_convergence(self):
        self.assertEqual(self.manifest["convergence"], "collect")

    def test_manifest_synth_ok_none(self):
        # collect mode never runs a synthesizer; synth_ok is always None by design.
        self.assertIsNone(self.manifest["synth_ok"])

    def test_manifest_judge_ok_none(self):
        self.assertIsNone(self.manifest["judge_ok"])

    def test_manifest_lanes_all_ok(self):
        self.assertEqual(len(self.manifest["lanes"]), 3)
        for lane in self.manifest["lanes"]:
            self.assertTrue(lane["ok"])

    def test_manifest_timing(self):
        _assert_timing(self, self.manifest)


# ---------------------------------------------------------------------------
# Scenario 5 — collect FAILED (all specialists fail)
# ---------------------------------------------------------------------------

class TestCollectFailed(unittest.TestCase):
    """collect mode — all specialists fail → FAILED."""

    def setUp(self):
        self.result, self.rendered, self.manifest = _run(
            _collect_config(),
            {"web": ("fail", "boom"), "social": ("fail", "boom"), "analysis": ("fail", "boom")},
        )

    def test_ok_is_false(self):
        self.assertFalse(self.result.ok)

    def test_render_header(self):
        self.assertIn("=== t — collect result — all specialists failed ===", self.rendered)

    def test_manifest_convergence(self):
        self.assertEqual(self.manifest["convergence"], "collect")

    def test_manifest_synth_ok_none(self):
        self.assertIsNone(self.manifest["synth_ok"])

    def test_manifest_judge_ok_none(self):
        self.assertIsNone(self.manifest["judge_ok"])

    def test_manifest_lanes_all_failed(self):
        self.assertEqual(len(self.manifest["lanes"]), 3)
        for lane in self.manifest["lanes"]:
            self.assertFalse(lane["ok"])

    def test_manifest_timing(self):
        _assert_timing(self, self.manifest)


# ---------------------------------------------------------------------------
# Scenario 6 — judge SUCCESS (all ok, judge ok)
# ---------------------------------------------------------------------------

class TestJudgeSuccess(unittest.TestCase):
    """judge mode — all specialists ok, judge ok → SUCCESS."""

    def setUp(self):
        self.result, self.rendered, self.manifest = _run(
            _judge_config(),
            {"judge": ("ok", "JUDGE GRADE TEXT")},
        )

    def test_ok_is_true(self):
        self.assertTrue(self.result.ok)

    def test_render_header(self):
        # Exact success header — no "failed" suffix
        self.assertIn("=== t — judge result ===", self.rendered)

    def test_render_not_judge_failed_header(self):
        self.assertNotIn("judge failed", self.rendered)

    def test_render_not_all_specialists_failed_header(self):
        self.assertNotIn("all specialists failed", self.rendered)

    def test_manifest_convergence(self):
        self.assertEqual(self.manifest["convergence"], "judge")

    def test_manifest_judge_ok_true(self):
        self.assertIs(self.manifest["judge_ok"], True)

    def test_manifest_synth_ok_none(self):
        self.assertIsNone(self.manifest["synth_ok"])

    def test_manifest_lanes_all_ok(self):
        self.assertEqual(len(self.manifest["lanes"]), 2)
        for lane in self.manifest["lanes"]:
            self.assertTrue(lane["ok"])

    def test_manifest_timing(self):
        _assert_timing(self, self.manifest)


# ---------------------------------------------------------------------------
# Scenario 7 — judge DEGRADED (judge fails, specialists ok)
# ---------------------------------------------------------------------------

class TestJudgeDegraded(unittest.TestCase):
    """judge mode — judge fails, specialists ok → DEGRADED."""

    def setUp(self):
        self.result, self.rendered, self.manifest = _run(
            _judge_config(),
            {"judge": ("fail", "judge-error")},
        )

    def test_ok_is_false(self):
        self.assertFalse(self.result.ok)

    def test_render_header(self):
        self.assertIn("=== t — judge result — judge failed ===", self.rendered)

    def test_render_not_all_specialists_failed(self):
        # Specialists survived; this is a degrade, not a total failure.
        self.assertNotIn("all specialists failed", self.rendered)

    def test_manifest_convergence(self):
        self.assertEqual(self.manifest["convergence"], "judge")

    def test_manifest_judge_ok_false(self):
        self.assertIs(self.manifest["judge_ok"], False)

    def test_manifest_synth_ok_none(self):
        self.assertIsNone(self.manifest["synth_ok"])

    def test_manifest_lanes_all_ok(self):
        # Specialists all succeeded; degradation was in the judge only.
        for lane in self.manifest["lanes"]:
            self.assertTrue(lane["ok"])

    def test_manifest_timing(self):
        _assert_timing(self, self.manifest)


# ---------------------------------------------------------------------------
# Scenario 8 — judge FAILED (all specialists fail)
# ---------------------------------------------------------------------------

class TestJudgeFailed(unittest.TestCase):
    """judge mode — all specialists fail → FAILED."""

    def setUp(self):
        # _judge_config has 2 specialists: "web" and "social"
        self.result, self.rendered, self.manifest = _run(
            _judge_config(),
            {"web": ("fail", "boom"), "social": ("fail", "boom")},
        )

    def test_ok_is_false(self):
        self.assertFalse(self.result.ok)

    def test_render_header(self):
        self.assertIn("=== t — judge result — all specialists failed ===", self.rendered)

    def test_manifest_convergence(self):
        self.assertEqual(self.manifest["convergence"], "judge")

    def test_manifest_judge_ok_none(self):
        # judge_ok is None → judge was never invoked (all specialists failed first).
        self.assertIsNone(self.manifest["judge_ok"])

    def test_manifest_synth_ok_none(self):
        self.assertIsNone(self.manifest["synth_ok"])

    def test_manifest_lanes_all_failed(self):
        self.assertEqual(len(self.manifest["lanes"]), 2)
        for lane in self.manifest["lanes"]:
            self.assertFalse(lane["ok"])

    def test_manifest_timing(self):
        _assert_timing(self, self.manifest)


if __name__ == "__main__":
    unittest.main()
