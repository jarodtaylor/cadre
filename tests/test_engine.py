import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from fleet_engine.config import FleetConfig
from fleet_engine.engine import run_fleet
from fleet_engine.model_client import AgentResult


def _config(**overrides):
    data = {
        "name": "t",
        "synthesis": {"provider": "openrouter", "model": "synth/model", "prompt": "SYNTH:"},
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "web/model", "toolset": ["web"]},
            {"role": "social", "provider": "xai", "model": "grok", "toolset": ["x_search"]},
            {"role": "analysis", "provider": "openrouter", "model": "ana/model", "toolset": ["web"]},
        ],
    }
    data.update(overrides)
    return FleetConfig.from_dict(data)


class FakeClient:
    """Duck-typed ModelClient: behavior keyed by role -> ('ok', text) | ('fail', error)."""

    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.calls = []  # (role, prompt)

    def run(self, *, role, provider, model, prompt, toolset=()):
        self.calls.append((role, prompt))
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(
            role=role, provider=provider, model=model, ok=ok,
            text=payload if ok else None, error=None if ok else payload,
        )

    def prompt_for(self, role):
        return next(p for (r, p) in self.calls if r == role)


class TestHappyPath(unittest.TestCase):
    def test_all_succeed_then_synthesize(self):
        client = FakeClient({"synthesizer": ("ok", "FINAL REPORT")})
        result = run_fleet(_config(), "what's new in agents?", client)
        self.assertTrue(result.ok)
        self.assertEqual(result.synthesis, "FINAL REPORT")
        self.assertEqual(len(result.specialists), 3)
        self.assertEqual(len(result.successes), 3)
        self.assertEqual(result.failures, [])

    def test_synthesizer_receives_labeled_provenance(self):
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        run_fleet(_config(), "task", client)
        synth_prompt = client.prompt_for("synthesizer")
        for role in ("web", "social", "analysis"):
            self.assertIn(role, synth_prompt)
        for model in ("web/model", "grok", "ana/model"):
            self.assertIn(model, synth_prompt)

    def test_fan_out_one_call_per_specialist_then_synth(self):
        client = FakeClient({"synthesizer": ("ok", "x")})
        run_fleet(_config(), "task", client)
        roles = [r for (r, _) in client.calls]
        self.assertEqual(sorted(roles[:3]), ["analysis", "social", "web"])
        self.assertEqual(roles[3], "synthesizer")
        self.assertEqual(len(client.calls), 4)


class TestProvenance(unittest.TestCase):
    def test_each_specialist_labeled(self):
        client = FakeClient({"synthesizer": ("ok", "x")})
        result = run_fleet(_config(), "task", client)
        by_role = {r.role: r for r in result.specialists}
        self.assertEqual(by_role["social"].provider, "xai")
        self.assertEqual(by_role["social"].model, "grok")
        self.assertEqual(by_role["web"].provider, "openrouter")


class TestDegradation(unittest.TestCase):
    def test_partial_failure_synthesizes_survivors(self):
        client = FakeClient({"social": ("fail", "auth error"), "synthesizer": ("ok", "PARTIAL")})
        result = run_fleet(_config(), "task", client)
        self.assertTrue(result.ok)
        self.assertEqual(result.synthesis, "PARTIAL")
        self.assertEqual(len(result.failures), 1)
        self.assertTrue(any("social" in n and "failed" in n for n in result.notes))
        # the failed specialist's output is not fed to the synthesizer
        self.assertNotIn("social", client.prompt_for("synthesizer"))

    def test_total_failure_no_synthesis(self):
        behavior = {r: ("fail", "down") for r in ("web", "social", "analysis")}
        client = FakeClient(behavior)
        result = run_fleet(_config(), "task", client)
        self.assertFalse(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertTrue(any("all specialists failed" in n for n in result.notes))
        self.assertNotIn("synthesizer", [r for (r, _) in client.calls])

    def test_synthesizer_failure_keeps_labeled_outputs(self):
        client = FakeClient({"synthesizer": ("fail", "rate limited")})
        result = run_fleet(_config(), "task", client)
        self.assertFalse(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertEqual(len(result.successes), 3)  # specialist outputs preserved
        self.assertTrue(any("synthesizer failed" in n for n in result.notes))

    def test_single_survivor_is_flagged(self):
        client = FakeClient({
            "web": ("fail", "x"), "analysis": ("fail", "y"), "synthesizer": ("ok", "SOLO"),
        })
        result = run_fleet(_config(), "task", client)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.successes), 1)
        self.assertTrue(any("single surviving" in n for n in result.notes))


class TestSynthesisPromptFallback(unittest.TestCase):
    def test_default_prompt_used_when_prompt_empty(self):
        client = FakeClient({"synthesizer": ("ok", "x")})
        cfg = _config(synthesis={"provider": "openrouter", "model": "synth/model", "prompt": ""})
        run_fleet(cfg, "task", client)
        self.assertIn("Synthesize the specialist findings", client.prompt_for("synthesizer"))


class HangingClient:
    """Fake client where chosen roles hang forever — models a stuck provider.

    A hung call blocks on an Event that is never set; its daemon thread is
    abandoned at process exit. Non-hung roles return instantly.
    """

    def __init__(self, hang_roles=(), behavior=None):
        self.hang = set(hang_roles)
        self.behavior = behavior or {}
        self._never = threading.Event()

    def run(self, *, role, provider, model, prompt, toolset=()):
        if role in self.hang:
            self._never.wait()  # blocks until the process exits
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(
            role=role, provider=provider, model=model, ok=ok,
            text=payload if ok else None, error=None if ok else payload,
        )


class TestSpecialistTimeout(unittest.TestCase):
    def test_hung_specialist_times_out_and_survivors_synthesize(self):
        client = HangingClient(hang_roles={"social"}, behavior={"synthesizer": ("ok", "DONE")})
        start = time.monotonic()
        result = run_fleet(_config(), "task", client, call_timeout=0.3)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 2.0)  # returned despite the hang, ~call_timeout not forever
        social = next(r for r in result.specialists if r.role == "social")
        self.assertFalse(social.ok)
        self.assertIn("timed out", social.error)
        self.assertTrue(any("social" in n and "timed out" in n for n in result.notes))
        # Degraded: synthesized over the two survivors.
        self.assertTrue(result.ok)
        self.assertEqual(result.synthesis, "DONE")
        self.assertEqual(len(result.successes), 2)


class TestSynthesizerTimeout(unittest.TestCase):
    def test_hung_synthesizer_degrades_to_labeled_outputs(self):
        client = HangingClient(hang_roles={"synthesizer"})
        start = time.monotonic()
        result = run_fleet(_config(), "task", client, call_timeout=0.3)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 2.0)
        self.assertFalse(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertEqual(len(result.successes), 3)  # specialist outputs preserved
        self.assertTrue(any("synthesizer failed" in n and "timed out" in n for n in result.notes))


# Child program for the clean-exit test: run a fleet with one call hung forever,
# then print a sentinel. If run_fleet leaves a non-daemon thread (e.g. a revert to
# ThreadPoolExecutor), the interpreter joins it at shutdown and never exits — the
# parent's subprocess timeout then fires and the test fails. argv[1] picks what hangs.
_CLEAN_EXIT_CHILD = """
import sys, threading
from fleet_engine.config import FleetConfig
from fleet_engine.engine import run_fleet
from fleet_engine.model_client import AgentResult

hang = sys.argv[1]
never = threading.Event()


class HangingClient:
    def run(self, *, role, provider, model, prompt, toolset=()):
        if (hang == "synthesizer" and role == "synthesizer") or (hang == "specialist" and role == "social"):
            never.wait()
        return AgentResult(role=role, provider=provider, model=model, ok=True, text=role + "-out")


cfg = FleetConfig.from_dict({
    "name": "t",
    "synthesis": {"provider": "openrouter", "model": "s/m", "prompt": "S:"},
    "specialists": [
        {"role": "web", "provider": "openrouter", "model": "w/m"},
        {"role": "social", "provider": "xai", "model": "grok"},
    ],
})
run_fleet(cfg, "task", HangingClient(), call_timeout=0.3)
print("EXITED_CLEANLY")
"""


class TestCaptureFields(unittest.TestCase):
    """U1 capture signals: elapsed_s, toolset, timed_out on every lane; synth_ok on FleetResult."""

    def test_elapsed_s_is_non_negative_on_success(self):
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        result = run_fleet(_config(), "task", client)
        for lane in result.specialists:
            self.assertIsNotNone(lane.elapsed_s, f"{lane.role}: elapsed_s should not be None")
            self.assertGreaterEqual(lane.elapsed_s, 0.0, f"{lane.role}: elapsed_s must be >= 0")

    def test_elapsed_s_is_non_negative_on_error(self):
        client = FakeClient({"social": ("fail", "auth error"), "synthesizer": ("ok", "PARTIAL")})
        result = run_fleet(_config(), "task", client)
        for lane in result.specialists:
            self.assertIsNotNone(lane.elapsed_s, f"{lane.role}: elapsed_s should not be None")
            self.assertGreaterEqual(lane.elapsed_s, 0.0, f"{lane.role}: elapsed_s must be >= 0")

    def test_elapsed_s_is_non_negative_on_timeout(self):
        client = HangingClient(hang_roles={"social"}, behavior={"synthesizer": ("ok", "DONE")})
        result = run_fleet(_config(), "task", client, call_timeout=0.3)
        for lane in result.specialists:
            self.assertIsNotNone(lane.elapsed_s, f"{lane.role}: elapsed_s should not be None")
            self.assertGreaterEqual(lane.elapsed_s, 0.0, f"{lane.role}: elapsed_s must be >= 0")

    def test_toolset_matches_spec_toolset(self):
        client = FakeClient({"synthesizer": ("ok", "x")})
        result = run_fleet(_config(), "task", client)
        by_role = {r.role: r for r in result.specialists}
        # spec toolsets from _config(): web=["web"], social=["x_search"], analysis=["web"]
        self.assertEqual(by_role["web"].toolset, ["web"])
        self.assertEqual(by_role["social"].toolset, ["x_search"])
        self.assertEqual(by_role["analysis"].toolset, ["web"])

    def test_no_toolset_specialist_yields_empty_list_not_none(self):
        cfg_data = {
            "name": "t",
            "synthesis": {"provider": "openrouter", "model": "synth/model", "prompt": "S:"},
            "specialists": [
                {"role": "notool", "provider": "openrouter", "model": "m/m"},
            ],
        }
        cfg = FleetConfig.from_dict(cfg_data)
        client = FakeClient({"synthesizer": ("ok", "x")})
        result = run_fleet(cfg, "task", client)
        lane = result.specialists[0]
        self.assertEqual(lane.toolset, [])
        self.assertIsNotNone(lane.toolset)  # must be [], never None

    def test_timed_out_true_only_for_hung_lane(self):
        client = HangingClient(hang_roles={"social"}, behavior={"synthesizer": ("ok", "DONE")})
        result = run_fleet(_config(), "task", client, call_timeout=0.3)
        by_role = {r.role: r for r in result.specialists}
        self.assertTrue(by_role["social"].timed_out, "hung lane must have timed_out=True")
        self.assertFalse(by_role["web"].timed_out, "non-hung lane must have timed_out=False")
        self.assertFalse(by_role["analysis"].timed_out, "non-hung lane must have timed_out=False")

    def test_timed_out_false_on_all_success(self):
        client = FakeClient({"synthesizer": ("ok", "x")})
        result = run_fleet(_config(), "task", client)
        for lane in result.specialists:
            self.assertFalse(lane.timed_out, f"{lane.role}: no timeout should have timed_out=False")

    def test_timed_out_false_on_typed_failure(self):
        client = FakeClient({"social": ("fail", "auth error"), "synthesizer": ("ok", "x")})
        result = run_fleet(_config(), "task", client)
        social = next(r for r in result.specialists if r.role == "social")
        self.assertFalse(social.timed_out, "typed failure (not timeout) must have timed_out=False")

    def test_synth_ok_true_on_synthesis_success(self):
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        result = run_fleet(_config(), "task", client)
        self.assertTrue(result.ok)
        self.assertIs(result.synth_ok, True)

    def test_synth_ok_false_when_synthesizer_fails(self):
        client = FakeClient({"synthesizer": ("fail", "rate limited")})
        result = run_fleet(_config(), "task", client)
        self.assertFalse(result.ok)
        self.assertIs(result.synth_ok, False)

    def test_synth_ok_none_when_synthesis_not_attempted(self):
        # All specialists fail — synthesizer is never called
        behavior = {r: ("fail", "down") for r in ("web", "social", "analysis")}
        client = FakeClient(behavior)
        result = run_fleet(_config(), "task", client)
        self.assertFalse(result.ok)
        self.assertIsNone(result.synth_ok)

    def test_synth_ok_false_on_synthesizer_timeout(self):
        client = HangingClient(hang_roles={"synthesizer"})
        result = run_fleet(_config(), "task", client, call_timeout=0.3)
        self.assertFalse(result.ok)
        self.assertIs(result.synth_ok, False)


class TestCleanExitOnHang(unittest.TestCase):
    """The property that justified daemon threads: the process EXITS on a hang.

    An in-process test can't observe interpreter shutdown, so this runs run_fleet
    in a child and asserts it exits promptly even with a provider hung forever.
    """

    def _assert_exits(self, hang):
        repo_root = Path(__file__).resolve().parents[1]
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _CLEAN_EXIT_CHILD, hang],
                cwd=repo_root, capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"run_fleet did not exit with a hung {hang} — a non-daemon thread was joined at shutdown")
        self.assertIn("EXITED_CLEANLY", proc.stdout, msg=proc.stderr)

    def test_exits_with_hung_specialist(self):
        self._assert_exits("specialist")

    def test_exits_with_hung_synthesizer(self):
        self._assert_exits("synthesizer")


if __name__ == "__main__":
    unittest.main()
