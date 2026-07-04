import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult, FleetStatus, run_fleet
from fleet_engine.model_client import AgentResult
from fleet_engine.personas import resolve
from fleet_engine.progress import JudgeDone, JudgeStarted, LaneDone, LaneLaunched, RoundStarted


def _config(**overrides):
    data = {
        "name": "t",
        "synthesis": {"provider": "openrouter", "model": "synth/model", "prompt": "SYNTH:"},
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "web/model", "toolset": ["web"],
             "focus": "web research"},
            {"role": "social", "provider": "xai", "model": "grok", "toolset": ["x_search"],
             "focus": "social scan"},
            {"role": "analysis", "provider": "openrouter", "model": "ana/model", "toolset": ["web"],
             "focus": "deep analysis"},
        ],
    }
    data.update(overrides)
    cfg = FleetConfig.from_dict(data)
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


class FakeClient:
    """Duck-typed ModelClient: behavior keyed by role -> ('ok', text) | ('fail', error)."""

    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.calls = []         # (role, prompt)
        self.toolset_for = {}   # role -> toolset of last call (for toolset-assertion tests)

    def run(self, *, role, provider, model, prompt, toolset=()):
        self.calls.append((role, prompt))
        self.toolset_for[role] = toolset
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(
            role=role, provider=provider, model=model, ok=ok,
            text=payload if ok else None, error=None if ok else payload,
        )

    def prompt_for(self, role):
        return next(p for (r, p) in self.calls if r == role)


def _derive_status(ok, synth_ok, judge_ok, convergence):
    """Shim: derive FleetStatus from the legacy ok/synth_ok/judge_ok kwargs used by test factories.

    Keeps factory external signatures unchanged while passing status= to FleetResult.
    Used by make_result (test_render.py), _result/_collect_result/_judge_result (test_capture.py),
    and directly here. Must stay in sync with the engine's status-assignment logic.
    """
    if ok:
        return FleetStatus.SUCCESS
    if convergence == "synthesize" and synth_ok is False:
        return FleetStatus.DEGRADED
    if convergence == "judge" and judge_ok is False:
        return FleetStatus.DEGRADED
    return FleetStatus.FAILED


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

    def test_parallel_topology_tag(self):
        """AE4: a default parallel fleet run carries topology='parallel' on the result."""
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        result = run_fleet(_config(), "what's new in agents?", client)
        self.assertEqual(result.topology, "parallel")


class TestPersonaPrompt(unittest.TestCase):
    """Engine reads effective_instruction for specialist prompts.

    After U3, _specialist_prompt reads spec.effective_instruction, not spec.focus.
    For a persona spec, effective_instruction is the full persona body (set by resolve()).
    The engine must pass that body through to the model — never the empty raw focus.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.pool = os.path.realpath(self._tmp)
        persona_body = "You are a coherence reviewer. Look for internal contradictions.\n"
        with open(os.path.join(self.pool, "coherence.md"), "w", encoding="utf-8") as f:
            f.write(persona_body)
        self.persona_body = persona_body

        data = {
            "name": "t",
            "convergence": "collect",
            "specialists": [
                {"role": "coherence", "provider": "openrouter", "model": "m/m",
                 "persona": "coherence", "toolset": []},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, self.pool)
        self.cfg = cfg

    def tearDown(self):
        shutil.rmtree(self._tmp)

    def test_persona_body_in_specialist_prompt(self):
        """effective_instruction (persona body) appears in the prompt sent to the model."""
        client = FakeClient()
        run_fleet(self.cfg, "test task", client)
        prompt = client.prompt_for("coherence")
        self.assertIn("You are a coherence reviewer.", prompt,
                      "persona body must appear in the specialist prompt")
        self.assertIn("Look for internal contradictions.", prompt)

    def test_empty_focus_not_in_persona_specialist_prompt(self):
        """A persona spec has focus=''. The empty focus must not appear in the prompt
        (confirms we read effective_instruction, not raw focus, which would show 'Focus: ')."""
        client = FakeClient()
        run_fleet(self.cfg, "test task", client)
        prompt = client.prompt_for("coherence")
        # With effective_instruction set (truthy), the prompt carries "Focus: <body>".
        # If the engine had read raw focus (empty string), no Focus: line would appear.
        self.assertIn("Focus:", prompt, "effective_instruction must appear under Focus: label")

    def test_run_fleet_rejects_unresolved_instruction(self):
        """KTD8 defense-in-depth: run_fleet fails loud when a spec was never resolved
        (effective_instruction empty) rather than silently dropping the instruction and
        running a no-instruction lane. A persona spec has focus='' by XOR, so without
        the guard an unresolved persona lane is indistinguishable from a valid one."""
        unresolved = FleetConfig.from_dict({
            "name": "t",
            "convergence": "collect",
            "specialists": [
                {"role": "coherence", "provider": "p", "model": "m", "persona": "coherence"},
            ],
        })  # deliberately NOT resolve()'d — effective_instruction stays ""
        with self.assertRaises(ValueError) as ctx:
            run_fleet(unresolved, "task", FakeClient())
        msg = str(ctx.exception)
        self.assertIn("resolve", msg.lower())
        self.assertIn("coherence", msg, "the error must name the offending specialist role")

    def test_run_fleet_accepts_focus_only_without_resolve(self):
        """Codex [high] regression: a focus-only fleet runs straight from FleetConfig
        load/from_dict with NO resolve() — effective_instruction is populated from focus
        at parse, so the pre-persona engine API (load -> run_fleet) keeps working."""
        cfg = FleetConfig.from_dict({
            "name": "t",
            "convergence": "collect",
            "specialists": [
                {"role": "web", "provider": "p", "model": "m", "toolset": [], "focus": "find sources"},
            ],
        })  # deliberately NOT resolve()'d
        client = FakeClient()
        result = run_fleet(cfg, "task", client)  # must not raise
        self.assertTrue(result.ok)
        self.assertIn(
            "find sources", client.prompt_for("web"),
            "focus must reach the prompt via parse-time effective_instruction",
        )


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
from fleet_engine.personas import resolve

hang = sys.argv[1]
never = threading.Event()


class HangingClient:
    def run(self, *, role, provider, model, prompt, toolset=()):
        if (hang == "synthesizer" and role == "synthesizer") or \
           (hang == "specialist" and role == "social") or \
           (hang == "judge" and role == "judge"):
            never.wait()
        return AgentResult(role=role, provider=provider, model=model, ok=True, text=role + "-out")


if hang == "judge":
    cfg_data = {
        "name": "t",
        "convergence": "judge",
        "judge": {"provider": "openrouter", "model": "j/m"},
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "w/m", "focus": "web research"},
            {"role": "social", "provider": "xai", "model": "grok", "focus": "social scan"},
        ],
    }
else:
    cfg_data = {
        "name": "t",
        "synthesis": {"provider": "openrouter", "model": "s/m", "prompt": "S:"},
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "w/m", "focus": "web research"},
            {"role": "social", "provider": "xai", "model": "grok", "focus": "social scan"},
        ],
    }
cfg = FleetConfig.from_dict(cfg_data)
resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O (mirrors prod callers)
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
                {"role": "notool", "provider": "openrouter", "model": "m/m", "focus": "analysis only"},
            ],
        }
        cfg = FleetConfig.from_dict(cfg_data)
        resolve(cfg, "/unused")
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

    def test_exits_with_hung_judge(self):
        self._assert_exits("judge")


def _judge_config(**overrides):
    """Build a judge-convergence FleetConfig (no synthesis block required)."""
    data = {
        "name": "t",
        "convergence": "judge",
        "judge": {"provider": "openrouter", "model": "judge/model"},
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "web/model", "toolset": ["web"],
             "focus": "web research"},
            {"role": "social", "provider": "xai", "model": "grok", "toolset": ["x_search"],
             "focus": "social scan"},
        ],
    }
    data.update(overrides)
    cfg = FleetConfig.from_dict(data)
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


def _collect_config(**overrides):
    """Build a collect-convergence FleetConfig (no synthesis block required)."""
    data = {
        "name": "t",
        "convergence": "collect",
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "web/model", "toolset": ["web"],
             "focus": "web research"},
            {"role": "social", "provider": "xai", "model": "grok", "toolset": ["x_search"],
             "focus": "social scan"},
            {"role": "analysis", "provider": "openrouter", "model": "ana/model", "toolset": ["web"],
             "focus": "deep analysis"},
        ],
    }
    data.update(overrides)
    cfg = FleetConfig.from_dict(data)
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


class TestCollectConvergence(unittest.TestCase):
    """Collect mode: fan-out without synthesis, convergence-aware ok."""

    def test_collect_all_succeed_no_synthesizer_called(self):
        """Synthesizer is never invoked when convergence=collect and all specialists succeed."""
        client = FakeClient()
        result = run_fleet(_collect_config(), "task", client)

        # Synthesizer must never have been called
        called_roles = [r for (r, _) in client.calls]
        self.assertNotIn("synthesizer", called_roles)

        self.assertTrue(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertIsNone(result.synth_ok)
        self.assertEqual(result.convergence, "collect")
        self.assertEqual(len(result.specialists), 3)
        self.assertEqual(len(result.successes), 3)

    def test_collect_partial_failure_ok_true(self):
        """2 of 3 specialists succeed → ok=True, both success results returned, one failure note."""
        client = FakeClient({"social": ("fail", "auth error")})
        result = run_fleet(_collect_config(), "task", client)

        called_roles = [r for (r, _) in client.calls]
        self.assertNotIn("synthesizer", called_roles)

        self.assertTrue(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertIsNone(result.synth_ok)
        self.assertEqual(result.convergence, "collect")
        self.assertEqual(len(result.successes), 2)
        self.assertEqual(len(result.failures), 1)
        self.assertTrue(any("social" in n and "failed" in n for n in result.notes))

    def test_collect_all_fail_ok_false(self):
        """All specialists fail → ok=False, synthesizer never called, convergence=collect."""
        behavior = {r: ("fail", "down") for r in ("web", "social", "analysis")}
        client = FakeClient(behavior)
        result = run_fleet(_collect_config(), "task", client)

        called_roles = [r for (r, _) in client.calls]
        self.assertNotIn("synthesizer", called_roles)

        self.assertFalse(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertIsNone(result.synth_ok)
        self.assertEqual(result.convergence, "collect")
        self.assertEqual(len(result.failures), 3)

    def test_synthesize_fleet_carries_convergence_synthesize(self):
        """Synthesize mode (default) carries convergence="synthesize" on the result (regression guard)."""
        client = FakeClient({"synthesizer": ("ok", "FINAL REPORT")})
        result = run_fleet(_config(), "task", client)

        self.assertTrue(result.ok)
        self.assertEqual(result.synthesis, "FINAL REPORT")
        self.assertEqual(result.convergence, "synthesize")

    def test_synthesize_all_fail_carries_convergence_synthesize(self):
        """All-failed synthesize run carries convergence="synthesize" (distinguishable from collect all-fail)."""
        behavior = {r: ("fail", "down") for r in ("web", "social", "analysis")}
        client = FakeClient(behavior)
        result = run_fleet(_config(), "task", client)

        self.assertFalse(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertEqual(result.convergence, "synthesize")

    def test_collect_slow_hook_does_not_cause_false_timeouts(self):
        """Collect mode: a slow progress hook does not falsely time out completed lanes.

        Mirrors TestSlowHookDoesNotCauseFalseTimeouts in test_progress.py.
        The three-phase drain is preserved verbatim in collect mode — this test
        guards that collect does not introduce a simplified drain that would
        reintroduce the deadline-vs-callback-latency bug.
        """
        def slow_hook(event):
            if isinstance(event, LaneDone):
                time.sleep(0.25)  # > call_timeout; models stalled capture I/O

        # All specialists return instantly; the hook (not the model) is slow.
        client = FakeClient()
        result = run_fleet(_collect_config(), "task", client, call_timeout=0.1, progress=slow_hook)

        # Synthesizer never called in collect mode
        called_roles = [r for (r, _) in client.calls]
        self.assertNotIn("synthesizer", called_roles)

        # All lanes completed in time — none should be falsely marked timed_out
        for lane in result.specialists:
            self.assertFalse(lane.timed_out, f"{lane.role} completed but was marked timed_out")
            self.assertTrue(lane.ok, f"{lane.role} should be a success")

        self.assertEqual(len(result.successes), 3)
        self.assertTrue(result.ok)
        self.assertEqual(result.convergence, "collect")

        # elapsed is from the worker's completion stamp, not inflated by hook latency
        for lane in result.specialists:
            self.assertLess(lane.elapsed_s, 0.2, f"{lane.role} elapsed inflated by hook latency")


class TestJudgeConvergence(unittest.TestCase):
    """Judge mode: one independent critic call over surviving specialists."""

    def test_judge_succeeds_result_fields(self):
        """Judge succeeds → judge set, judge_ok True, ok True, convergence 'judge'."""
        client = FakeClient()
        result = run_fleet(_judge_config(), "task", client)

        self.assertTrue(result.ok)
        self.assertEqual(result.judge, "judge-output")
        self.assertIs(result.judge_ok, True)
        self.assertEqual(result.convergence, "judge")
        # synthesis fields stay unset (KTD2)
        self.assertIsNone(result.synthesis)
        self.assertIsNone(result.synth_ok)

    def test_judge_succeeds_emits_progress_events(self):
        """JudgeStarted and JudgeDone are emitted on a successful judge run."""
        events = []
        client = FakeClient()
        run_fleet(_judge_config(), "task", client, progress=lambda e: events.append(e))

        started = [e for e in events if isinstance(e, JudgeStarted)]
        done = [e for e in events if isinstance(e, JudgeDone)]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(done), 1)
        self.assertEqual(started[0].survivors, 2)  # 2 specialists in _judge_config
        self.assertEqual(done[0].outcome, "ok")
        self.assertIsNotNone(done[0].elapsed_s)

    def test_judge_failure_degrades_gracefully(self):
        """Judge call returns failure → judge_ok False, judge None, ok False, degrade note."""
        client = FakeClient({"judge": ("fail", "rate limited")})
        result = run_fleet(_judge_config(), "task", client)

        self.assertFalse(result.ok)
        self.assertIsNone(result.judge)
        self.assertIs(result.judge_ok, False)
        self.assertTrue(any("judge failed" in n for n in result.notes))
        # Specialist outputs preserved for attribution (R10)
        self.assertEqual(len(result.successes), 2)

    def test_judge_timeout_degrades_gracefully(self):
        """Hung judge times out → judge_ok False, ok False, timed-out note, no hang."""
        client = HangingClient(hang_roles={"judge"})
        start = time.monotonic()
        result = run_fleet(_judge_config(), "task", client, call_timeout=0.3)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 2.0)  # returned despite the hang
        self.assertFalse(result.ok)
        self.assertIsNone(result.judge)
        self.assertIs(result.judge_ok, False)
        self.assertTrue(any("judge failed" in n and "timed out" in n for n in result.notes))

    def test_all_specialists_fail_judge_not_invoked(self):
        """All specialists fail → judge never invoked, judge_ok None, ok False."""
        behavior = {r: ("fail", "down") for r in ("web", "social")}
        client = FakeClient(behavior)
        result = run_fleet(_judge_config(), "task", client)

        self.assertFalse(result.ok)
        self.assertIsNone(result.judge)
        self.assertIsNone(result.judge_ok)
        called_roles = [r for (r, _) in client.calls]
        self.assertNotIn("judge", called_roles)
        # The all-fail note is judge-specific, not synthesize-only wording (bot review).
        self.assertTrue(any("all specialists failed" in n for n in result.notes))
        self.assertFalse(any("no synthesis" in n for n in result.notes))
        self.assertTrue(any("no judge grade" in n for n in result.notes))

    def test_judge_client_receives_explicit_empty_toolset(self):
        """The judge's client.run receives toolset=[] — not None, not () (KTD6/R8)."""
        client = FakeClient()
        run_fleet(_judge_config(), "task", client)

        self.assertIn("judge", client.toolset_for, "judge role must have been called")
        received = client.toolset_for["judge"]
        self.assertEqual(received, [], "judge must receive toolset=[], not None or ()")
        self.assertIsNotNone(received)
        self.assertIsInstance(received, list)

    def test_judge_prompt_labels_survivors_by_exact_role(self):
        """_judge_prompt labels each survivor by its exact role string (KTD9)."""
        client = FakeClient()
        cfg = _judge_config()
        run_fleet(cfg, "task", client)
        judge_prompt = client.prompt_for("judge")
        for spec in cfg.specialists:
            self.assertIn(spec.role, judge_prompt,
                          f"exact role {spec.role!r} must appear in the judge prompt")

    def test_judge_prompt_contains_format_tokens(self):
        """_judge_prompt contains the three format-contract tokens the parser relies on."""
        client = FakeClient()
        run_fleet(_judge_config(), "task", client)
        judge_prompt = client.prompt_for("judge")
        self.assertIn("=== LANE:", judge_prompt)
        self.assertIn("Grade:", judge_prompt)
        self.assertIn("Rationale:", judge_prompt)

    def test_judge_does_not_invoke_synthesizer(self):
        """Judge convergence never invokes the synthesizer role."""
        client = FakeClient()
        run_fleet(_judge_config(), "task", client)
        called_roles = [r for (r, _) in client.calls]
        self.assertNotIn("synthesizer", called_roles)

    def test_judge_partial_failure_still_invokes_judge(self):
        """One specialist fails but one survives → judge still runs over the survivor."""
        client = FakeClient({"social": ("fail", "error")})
        result = run_fleet(_judge_config(), "task", client)

        self.assertTrue(result.ok)
        self.assertIs(result.judge_ok, True)
        # Only the surviving specialist's output went to the judge
        events = []
        run_fleet(_judge_config(specialists=[
            {"role": "web", "provider": "openrouter", "model": "web/model",
             "toolset": ["web"], "focus": "web research"},
            {"role": "social", "provider": "xai", "model": "grok",
             "toolset": ["x_search"], "focus": "social scan"},
        ]), "task", FakeClient({"social": ("fail", "down")}),
            progress=lambda e: events.append(e))
        started = [e for e in events if isinstance(e, JudgeStarted)]
        self.assertEqual(started[0].survivors, 1)


class TestFleetStatus(unittest.TestCase):
    """status field, ok property, and has_usable_output() across the eight convergence/outcome states (synthesize 3, collect 2, judge 3)."""

    # ------------------------------------------------------------------
    # ok ≡ (status is SUCCESS) — per-mode return points
    # ------------------------------------------------------------------

    def test_synthesize_ok_is_success(self):
        result = run_fleet(_config(), "t", FakeClient({"synthesizer": ("ok", "FINAL")}))
        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertTrue(result.ok)

    def test_synthesize_synth_fail_is_degraded(self):
        result = run_fleet(_config(), "t", FakeClient({"synthesizer": ("fail", "err")}))
        self.assertIs(result.status, FleetStatus.DEGRADED)
        self.assertFalse(result.ok)

    def test_synthesize_all_fail_is_failed(self):
        # All three specialists fail → no synthesis attempted → FAILED
        result = run_fleet(
            _config(), "t",
            FakeClient({"web": ("fail", "e"), "social": ("fail", "e"), "analysis": ("fail", "e")}),
        )
        self.assertIs(result.status, FleetStatus.FAILED)
        self.assertFalse(result.ok)

    def test_collect_ok_is_success(self):
        result = run_fleet(_collect_config(), "t", FakeClient())
        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertTrue(result.ok)

    def test_collect_all_fail_is_failed(self):
        result = run_fleet(
            _collect_config(), "t",
            FakeClient({"web": ("fail", "e"), "social": ("fail", "e"), "analysis": ("fail", "e")}),
        )
        self.assertIs(result.status, FleetStatus.FAILED)
        self.assertFalse(result.ok)

    def test_judge_ok_is_success(self):
        result = run_fleet(_judge_config(), "t", FakeClient({"judge": ("ok", "GRADES")}))
        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertTrue(result.ok)

    def test_judge_fail_is_degraded(self):
        result = run_fleet(_judge_config(), "t", FakeClient({"judge": ("fail", "err")}))
        self.assertIs(result.status, FleetStatus.DEGRADED)
        self.assertFalse(result.ok)

    def test_judge_all_fail_is_failed(self):
        result = run_fleet(
            _judge_config(), "t",
            FakeClient({"web": ("fail", "e"), "social": ("fail", "e")}),
        )
        self.assertIs(result.status, FleetStatus.FAILED)
        self.assertFalse(result.ok)

    # ------------------------------------------------------------------
    # collect never produces DEGRADED (no convergence step can fail)
    # ------------------------------------------------------------------

    def test_collect_status_never_degraded(self):
        for behavior in ({}, {"web": ("fail", "e")}):
            with self.subTest(behavior=behavior):
                result = run_fleet(_collect_config(), "t", FakeClient(behavior))
                self.assertIsNot(result.status, FleetStatus.DEGRADED)

    # ------------------------------------------------------------------
    # has_usable_output(): True for SUCCESS and DEGRADED, False for FAILED
    # ------------------------------------------------------------------

    def test_has_usable_output_success(self):
        result = run_fleet(_config(), "t", FakeClient({"synthesizer": ("ok", "FINAL")}))
        self.assertTrue(result.has_usable_output())

    def test_has_usable_output_degraded(self):
        result = run_fleet(_config(), "t", FakeClient({"synthesizer": ("fail", "err")}))
        self.assertTrue(result.has_usable_output())

    def test_has_usable_output_failed(self):
        result = run_fleet(
            _config(), "t",
            FakeClient({"web": ("fail", "e"), "social": ("fail", "e"), "analysis": ("fail", "e")}),
        )
        self.assertFalse(result.has_usable_output())

    # ------------------------------------------------------------------
    # ok is a read-only property — no setter
    # ------------------------------------------------------------------

    def test_ok_property_has_no_setter(self):
        result = run_fleet(_config(), "t", FakeClient({"synthesizer": ("ok", "x")}))
        with self.assertRaises(AttributeError):
            result.ok = False  # type: ignore[misc]

    # ------------------------------------------------------------------
    # status is coerced to the enum — a raw string (e.g. the manifest's
    # serialized form) must read identically, because the derived reads
    # use `is` (identity), not `==`
    # ------------------------------------------------------------------

    def test_status_string_is_coerced_to_enum(self):
        # The manifest serializes status as a bare string ("success"/"degraded"/
        # "failed"). Reconstructed with that raw string, a result must behave
        # exactly like the enum-built one. Without coercion, `is` misreads on
        # two of the three values: "success" -> ok wrongly False; "failed" ->
        # has_usable_output() wrongly True.
        for value in ("success", "degraded", "failed"):
            with self.subTest(value=value):
                from_str = FleetResult(fleet="f", task="t", specialists=[], status=value)
                from_enum = FleetResult(fleet="f", task="t", specialists=[], status=FleetStatus(value))
                self.assertIs(from_str.status, FleetStatus(value))
                self.assertEqual(from_str.ok, from_enum.ok)
                self.assertEqual(from_str.has_usable_output(), from_enum.has_usable_output())

    def test_status_invalid_string_raises(self):
        with self.assertRaises(ValueError):
            FleetResult(fleet="f", task="t", specialists=[], status="bogus")


class TestU2ResultTypePlumbing(unittest.TestCase):
    """U2 result-type plumbing: skipped lane state, topology/terminal_produced on FleetResult.

    These tests cover the additive fields only — no execution logic, no chain logic.
    All existing parallel-path behaviour must be unaffected (verified by the full suite).
    """

    # ------------------------------------------------------------------
    # AgentResult.skipped field
    # ------------------------------------------------------------------

    def test_skipped_default_false(self):
        """skipped defaults to False — mirrors timed_out; existing callers unaffected."""
        r = AgentResult(role="web", provider="p", model="m", ok=True, text="out")
        self.assertFalse(r.skipped)

    def test_skipped_set_true(self):
        """skipped=True can be constructed; used by _run_chain (U3) for unrun lanes."""
        r = AgentResult(role="web", provider="p", model="m", ok=False, skipped=True)
        self.assertTrue(r.skipped)

    # ------------------------------------------------------------------
    # FleetResult.failures excludes skipped lanes (R11)
    # ------------------------------------------------------------------

    def test_failures_excludes_skipped_lane(self):
        """A skipped lane (ok=False, skipped=True) must not appear in failures.

        This is the load-bearing change: the failure-notes loop iterates
        result.failures, so once failures excludes skipped, no
        "specialist '<role>' failed" note is produced for the skipped lane.
        """
        skipped = AgentResult(role="skipped-lane", provider="p", model="m", ok=False, skipped=True)
        failed = AgentResult(role="real-fail", provider="p", model="m", ok=False, error="boom")
        result = FleetResult(
            fleet="f", task="t",
            specialists=[skipped, failed],
            status=FleetStatus.FAILED,
        )
        failure_roles = [r.role for r in result.failures]
        self.assertNotIn("skipped-lane", failure_roles,
                         "skipped lane must be excluded from failures")
        self.assertIn("real-fail", failure_roles,
                      "real failure must remain in failures")

    def test_skipped_vs_real_failure_distinguishable(self):
        """skipped and real-failure are distinct: only the real failure appears in failures."""
        skipped = AgentResult(role="s", provider="p", model="m", ok=False, skipped=True)
        real = AgentResult(role="r", provider="p", model="m", ok=False, skipped=False, error="err")
        result = FleetResult(
            fleet="f", task="t",
            specialists=[skipped, real],
            status=FleetStatus.FAILED,
        )
        self.assertTrue(skipped.skipped)
        self.assertFalse(real.skipped)
        # Only the real failure is in failures
        self.assertNotIn(skipped, result.failures)
        self.assertIn(real, result.failures)

    def test_all_skipped_failures_means_empty_failures(self):
        """If every non-ok lane is skipped, failures is empty."""
        lanes = [
            AgentResult(role=f"lane-{i}", provider="p", model="m", ok=False, skipped=True)
            for i in range(3)
        ]
        result = FleetResult(fleet="f", task="t", specialists=lanes, status=FleetStatus.FAILED)
        self.assertEqual(result.failures, [])

    # ------------------------------------------------------------------
    # The failure-notes loop omits skipped lanes (via failures exclusion)
    # ------------------------------------------------------------------

    def test_skipped_lane_produces_no_failure_note(self):
        """The run_fleet notes loop iterates result.failures. Since failures excludes
        skipped lanes, a skipped lane must produce no "specialist '...' failed" note.
        We verify this at the FleetResult level (the notes loop will consume failures).
        """
        skipped = AgentResult(role="skipped-role", provider="p", model="m", ok=False, skipped=True)
        result = FleetResult(
            fleet="f", task="t",
            specialists=[skipped],
            status=FleetStatus.FAILED,
        )
        # Simulate the notes loop (engine.py lines 406-407):
        # for failed in result.failures: result.notes.append(f"specialist '{failed.role}' failed: ...")
        for failed in result.failures:
            result.notes.append(f"specialist '{failed.role}' failed: {failed.error}")
        note_text = " ".join(result.notes)
        self.assertNotIn("skipped-role", note_text,
                         "skipped lane must not produce a failure note")

    # ------------------------------------------------------------------
    # FleetResult.topology field
    # ------------------------------------------------------------------

    def test_topology_defaults_to_parallel(self):
        """topology defaults to 'parallel' — existing FleetResult callers unaffected."""
        result = FleetResult(fleet="f", task="t", specialists=[], status=FleetStatus.FAILED)
        self.assertEqual(result.topology, "parallel")

    def test_topology_round_trips_sequential(self):
        """topology='sequential' can be constructed and read back (set by _run_chain in U3)."""
        result = FleetResult(
            fleet="f", task="t", specialists=[], status=FleetStatus.FAILED,
            topology="sequential",
        )
        self.assertEqual(result.topology, "sequential")

    # ------------------------------------------------------------------
    # FleetResult.terminal_produced field
    # ------------------------------------------------------------------

    def test_terminal_produced_defaults_to_none(self):
        """terminal_produced=None signals 'not a chain run' so the manifest builder
        never reads an undeclared attribute on a parallel result."""
        result = FleetResult(fleet="f", task="t", specialists=[], status=FleetStatus.FAILED)
        self.assertIsNone(result.terminal_produced)

    def test_terminal_produced_can_be_set_true(self):
        """terminal_produced=True: chain ran and the terminal lane produced output."""
        result = FleetResult(
            fleet="f", task="t", specialists=[], status=FleetStatus.SUCCESS,
            topology="sequential", terminal_produced=True,
        )
        self.assertTrue(result.terminal_produced)

    def test_terminal_produced_can_be_set_false(self):
        """terminal_produced=False: chain ran but the terminal lane was skipped or produced nothing."""
        result = FleetResult(
            fleet="f", task="t", specialists=[], status=FleetStatus.FAILED,
            topology="sequential", terminal_produced=False,
        )
        self.assertIs(result.terminal_produced, False)


def _chain_config(**overrides):
    """Build a 3-lane sequential+collect FleetConfig (the common chain test base).

    Roles: scout → analyst → writer, mirroring the research-brief flagship.
    No synthesis block — topology=sequential, convergence=collect by default.
    Use overrides to swap convergence to "synthesize" / add a synthesis block, or
    pass a different "specialists" list for shorter chains.
    """
    data = {
        "name": "t",
        "convergence": "collect",
        "topology": "sequential",
        "specialists": [
            {"role": "scout", "provider": "openrouter", "model": "s/m", "toolset": ["web"],
             "focus": "gather sources"},
            {"role": "analyst", "provider": "xai", "model": "grok", "toolset": ["web"],
             "focus": "audit and verify"},
            {"role": "writer", "provider": "openrouter", "model": "w/m", "toolset": [],
             "focus": "write prose"},
        ],
    }
    data.update(overrides)
    cfg = FleetConfig.from_dict(data)
    resolve(cfg, "/unused")
    return cfg


class TestSequentialTopology(unittest.TestCase):
    """_run_chain: serial execution, prompt threading, status logic, skipped lanes."""

    # ------------------------------------------------------------------
    # AE1: happy path — all 3 lanes succeed
    # ------------------------------------------------------------------

    def test_ae1_all_lanes_succeed(self):
        """All 3 lanes succeed → SUCCESS, terminal_produced=True, prompts contain prior output."""
        client = FakeClient()
        result = run_fleet(_chain_config(), "task", client)

        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertTrue(result.ok)
        self.assertTrue(result.terminal_produced)
        self.assertEqual(len(result.successes), 3)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.topology, "sequential")

        # Analyst's prompt must contain scout's output and the delimiter.
        analyst_prompt = client.prompt_for("analyst")
        self.assertIn("scout-output", analyst_prompt)
        self.assertIn("===== UPSTREAM STAGE: scout =====", analyst_prompt)

        # Writer's prompt must contain both prior outputs.
        writer_prompt = client.prompt_for("writer")
        self.assertIn("scout-output", writer_prompt)
        self.assertIn("analyst-output", writer_prompt)
        self.assertIn("===== UPSTREAM STAGE: scout =====", writer_prompt)
        self.assertIn("===== UPSTREAM STAGE: analyst =====", writer_prompt)

    def test_ae1_scout_prompt_has_no_prior_stages(self):
        """First lane gets only the base specialist prompt — no prior stages block."""
        client = FakeClient()
        run_fleet(_chain_config(), "task", client)
        scout_prompt = client.prompt_for("scout")
        self.assertNotIn("Prior chain stages", scout_prompt)
        self.assertNotIn("UPSTREAM STAGE", scout_prompt)

    def test_ae1_all_lanes_called_once_in_order(self):
        """Each lane called exactly once; no synthesizer call for collect topology."""
        client = FakeClient()
        run_fleet(_chain_config(), "task", client)
        called_roles = [r for (r, _) in client.calls]
        self.assertEqual(called_roles, ["scout", "analyst", "writer"])

    # ------------------------------------------------------------------
    # AE2: mid-chain failure — middle lane fails
    # ------------------------------------------------------------------

    def test_ae2_mid_chain_fail_breaks_chain(self):
        """Analyst fails → writer is skipped; status is DEGRADED; terminal_produced=False."""
        client = FakeClient({"analyst": ("fail", "error-msg")})
        result = run_fleet(_chain_config(), "task", client)

        self.assertIs(result.status, FleetStatus.DEGRADED)
        self.assertFalse(result.ok)
        self.assertIs(result.terminal_produced, False)

        by_role = {r.role: r for r in result.specialists}
        self.assertTrue(by_role["analyst"].ok is False)
        self.assertFalse(by_role["analyst"].skipped)   # analyst ran and failed (real failure)
        self.assertTrue(by_role["writer"].skipped)     # writer never ran

    def test_ae2_skipped_lane_not_in_failures(self):
        """Skipped writer must not appear in failures (excluded by design)."""
        client = FakeClient({"analyst": ("fail", "e")})
        result = run_fleet(_chain_config(), "task", client)
        failure_roles = [r.role for r in result.failures]
        self.assertIn("analyst", failure_roles)
        self.assertNotIn("writer", failure_roles)

    def test_ae2_skipped_lane_has_no_failure_note(self):
        """Skipped lane produces no 'specialist ... failed' note in notes."""
        client = FakeClient({"analyst": ("fail", "e")})
        result = run_fleet(_chain_config(), "task", client)
        note_text = " ".join(result.notes)
        self.assertIn("analyst", note_text)
        self.assertNotIn("writer", note_text)

    # ------------------------------------------------------------------
    # AE3: first-lane failure — chain halted immediately
    # ------------------------------------------------------------------

    def test_ae3_first_lane_fail_is_failed_status(self):
        """Scout fails → all downstream skipped; status=FAILED; no successes."""
        client = FakeClient({"scout": ("fail", "boom")})
        result = run_fleet(_chain_config(), "task", client)

        self.assertIs(result.status, FleetStatus.FAILED)
        self.assertEqual(result.successes, [])

        by_role = {r.role: r for r in result.specialists}
        self.assertFalse(by_role["scout"].skipped)  # scout ran and failed
        self.assertTrue(by_role["analyst"].skipped)
        self.assertTrue(by_role["writer"].skipped)

    def test_ae3_first_lane_fail_note_says_chain_halted(self):
        """'first lane failed — chain halted' note is appended on a first-lane failure."""
        client = FakeClient({"scout": ("fail", "e")})
        result = run_fleet(_chain_config(), "task", client)
        self.assertTrue(any("first lane failed" in n for n in result.notes))
        self.assertTrue(any("chain halted" in n for n in result.notes))

    # ------------------------------------------------------------------
    # AE7: terminal-lane failure — ran, but returned a failure
    # ------------------------------------------------------------------

    def test_ae7_terminal_lane_fail_is_degraded(self):
        """Writer fails (real failure, not skipped) → DEGRADED; terminal_produced=False."""
        client = FakeClient({"writer": ("fail", "rate-limited")})
        result = run_fleet(_chain_config(), "task", client)

        self.assertIs(result.status, FleetStatus.DEGRADED)
        self.assertIs(result.terminal_produced, False)

        by_role = {r.role: r for r in result.specialists}
        self.assertFalse(by_role["writer"].skipped)  # writer ran and failed (not skipped)
        self.assertFalse(by_role["writer"].ok)
        self.assertFalse(by_role["scout"].skipped)
        self.assertFalse(by_role["analyst"].skipped)

    # ------------------------------------------------------------------
    # AE6: non-terminal timeout — hung middle lane times out
    # ------------------------------------------------------------------

    def test_ae6_non_terminal_timeout_breaks_chain(self):
        """A hung analyst times out → writer is skipped; analyst.timed_out=True; chain DEGRADED."""
        client = HangingClient(hang_roles={"analyst"})
        start = time.monotonic()
        result = run_fleet(_chain_config(), "task", client, call_timeout=0.3)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 2.0)  # returned promptly despite the hang

        by_role = {r.role: r for r in result.specialists}
        self.assertTrue(by_role["analyst"].timed_out)
        self.assertTrue(by_role["writer"].skipped)
        self.assertIs(result.status, FleetStatus.DEGRADED)

    # ------------------------------------------------------------------
    # AE5: sequential + synthesize, healthy run
    # ------------------------------------------------------------------

    def test_ae5_sequential_synthesize_healthy(self):
        """3-lane chain with convergence=synthesize → synthesizer called once; SUCCESS."""
        data = {
            "name": "t",
            "convergence": "synthesize",
            "topology": "sequential",
            "synthesis": {"provider": "openrouter", "model": "synth/m"},
            "specialists": [
                {"role": "scout", "provider": "openrouter", "model": "s/m",
                 "toolset": ["web"], "focus": "gather"},
                {"role": "analyst", "provider": "xai", "model": "grok",
                 "toolset": ["web"], "focus": "analyze"},
                {"role": "writer", "provider": "openrouter", "model": "w/m",
                 "toolset": [], "focus": "write"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        client = FakeClient({"synthesizer": ("ok", "FINAL REPORT")})
        result = run_fleet(cfg, "task", client)

        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertEqual(result.synthesis, "FINAL REPORT")
        self.assertTrue(result.terminal_produced)
        synth_calls = [r for (r, _) in client.calls if r == "synthesizer"]
        self.assertEqual(len(synth_calls), 1)

    def test_sequential_synthesize_synth_fails_is_degraded(self):
        """Chain completes but synthesizer fails → DEGRADED; synthesis=None."""
        data = {
            "name": "t",
            "convergence": "synthesize",
            "topology": "sequential",
            "synthesis": {"provider": "openrouter", "model": "synth/m"},
            "specialists": [
                {"role": "scout", "provider": "openrouter", "model": "s/m",
                 "toolset": ["web"], "focus": "gather"},
                {"role": "analyst", "provider": "xai", "model": "grok",
                 "toolset": ["web"], "focus": "analyze"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        client = FakeClient({"synthesizer": ("fail", "rate limited")})
        result = run_fleet(cfg, "task", client)

        self.assertIs(result.status, FleetStatus.DEGRADED)
        self.assertIsNone(result.synthesis)

    def test_sequential_synthesize_mid_break_synth_still_degraded(self):
        """Chain breaks mid-way + synth succeeds over survivors → DEGRADED (conjunctive status)."""
        data = {
            "name": "t",
            "convergence": "synthesize",
            "topology": "sequential",
            "synthesis": {"provider": "openrouter", "model": "synth/m"},
            "specialists": [
                {"role": "scout", "provider": "openrouter", "model": "s/m",
                 "toolset": ["web"], "focus": "gather"},
                {"role": "analyst", "provider": "xai", "model": "grok",
                 "toolset": ["web"], "focus": "analyze"},
                {"role": "writer", "provider": "openrouter", "model": "w/m",
                 "toolset": [], "focus": "write"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        # Analyst fails → writer skipped → terminal_produced=False.
        # Synth runs over the sole survivor (scout). terminal_ok=False, so DEGRADED.
        client = FakeClient({"analyst": ("fail", "e"), "synthesizer": ("ok", "PARTIAL")})
        result = run_fleet(cfg, "task", client)

        self.assertIs(result.status, FleetStatus.DEGRADED)
        # Synthesizer should still have been called (chain has ≥1 survivor)
        synth_calls = [r for (r, _) in client.calls if r == "synthesizer"]
        self.assertEqual(len(synth_calls), 1)

    # ------------------------------------------------------------------
    # sequential + judge
    # ------------------------------------------------------------------

    def test_sequential_judge_healthy(self):
        """Sequential chain with convergence=judge → judge called once over survivors."""
        data = {
            "name": "t",
            "convergence": "judge",
            "topology": "sequential",
            "judge": {"provider": "openrouter", "model": "judge/m"},
            "specialists": [
                {"role": "scout", "provider": "openrouter", "model": "s/m",
                 "toolset": ["web"], "focus": "gather"},
                {"role": "analyst", "provider": "xai", "model": "grok",
                 "toolset": ["web"], "focus": "analyze"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        client = FakeClient({"judge": ("ok", "GRADES HERE")})
        result = run_fleet(cfg, "task", client)

        self.assertIsNotNone(result.judge)
        self.assertIs(result.judge_ok, True)
        judge_calls = [r for (r, _) in client.calls if r == "judge"]
        self.assertEqual(len(judge_calls), 1)

    # ------------------------------------------------------------------
    # Per-lane deadline — no false timeout (KTD5)
    # ------------------------------------------------------------------

    def test_per_lane_deadline_no_false_timeout(self):
        """Each lane gets its OWN fresh call_timeout — no starvation of later lanes.

        Band: t=0.1s per call, call_timeout=0.2s (> t, so each lane ok under its own
        deadline). A shared-deadline bug would give lane 3 ~0s remaining and time it
        out — the per-lane recompute prevents that. Tests all 3 succeed.
        """

        class SleepyClient:
            def __init__(self, delay):
                self.delay = delay

            def run(self, *, role, provider, model, prompt, toolset=()):
                time.sleep(self.delay)
                return AgentResult(role=role, provider=provider, model=model,
                                   ok=True, text=f"{role}-output")

        t = 0.1  # each lane takes ~0.1s
        client = SleepyClient(delay=t)
        result = run_fleet(_chain_config(), "task", client, call_timeout=0.2)

        self.assertEqual(len(result.successes), 3, "all 3 lanes must succeed with per-lane deadline")
        for lane in result.specialists:
            self.assertFalse(lane.timed_out, f"{lane.role} must not time out with per-lane deadline")
        self.assertIs(result.status, FleetStatus.SUCCESS)

    # ------------------------------------------------------------------
    # Per-stage truncation
    # ------------------------------------------------------------------

    def test_per_stage_truncation_flag(self):
        """Oversize single-unit stage output is hard-cut; threading_truncated=True; marker in prompt."""
        from fleet_engine.engine import CHAIN_STAGE_TOKEN_CAP, CHARS_PER_TOKEN

        char_budget = CHAIN_STAGE_TOKEN_CAP * CHARS_PER_TOKEN
        long_text = "X" * (char_budget + 100)   # one oversize unit (no blank lines) -> hard-cut

        class VariantClient:
            def __init__(self):
                self.calls = []

            def run(self, *, role, provider, model, prompt, toolset=()):
                self.calls.append((role, prompt))
                if role == "scout":
                    return AgentResult(role=role, provider=provider, model=model,
                                       ok=True, text=long_text)
                return AgentResult(role=role, provider=provider, model=model,
                                   ok=True, text=f"{role}-output")

        client = VariantClient()
        result = run_fleet(_chain_config(), "task", client)

        # threading_truncated must be True since scout's output exceeds the cap.
        self.assertTrue(result.threading_truncated)

        # Analyst's prompt must contain the truncation marker, not the full text.
        analyst_prompt = next(p for (r, p) in client.calls if r == "analyst")
        self.assertIn("[… stage output truncated …]", analyst_prompt)
        self.assertNotIn("X" * (char_budget + 1), analyst_prompt)
        # The injected block (content + truncation marker) stays within the char-equivalent
        # budget — the marker's room is reserved, not appended past it.
        self.assertLessEqual(analyst_prompt.count("X"), char_budget)

    def test_no_truncation_when_output_within_cap(self):
        """Short stage output does not set threading_truncated."""
        client = FakeClient()  # default outputs are short ("scout-output" etc.)
        result = run_fleet(_chain_config(), "task", client)
        self.assertFalse(result.threading_truncated)

    # ------------------------------------------------------------------
    # Whole-unit truncation of _cap_stage_text (#39, U2)
    # ------------------------------------------------------------------

    def test_cap_keeps_whole_units_and_drops_overflow(self):
        """Over-budget multi-unit text packs WHOLE units; the overflowing unit is dropped
        entirely (no mid-unit cut); marker present; truncated True (R4)."""
        from fleet_engine.engine import (
            _cap_stage_text, CHAIN_STAGE_TOKEN_CAP, CHARS_PER_TOKEN, _CHAIN_TRUNC_MARKER)

        unit_a, unit_b, unit_c = "A" * 7000, "B" * 7000, "C" * 7000
        text = "\n\n".join([unit_a, unit_b, unit_c])   # 21004 chars > 16000-char budget
        capped, truncated = _cap_stage_text(text)

        self.assertTrue(truncated)
        self.assertIn(unit_a, capped)          # first unit whole
        self.assertIn(unit_b, capped)          # second unit whole
        self.assertNotIn("C", capped)          # overflowing unit dropped entirely — no partial
        self.assertTrue(capped.endswith(_CHAIN_TRUNC_MARKER))
        self.assertLessEqual(len(capped), CHAIN_STAGE_TOKEN_CAP * CHARS_PER_TOKEN)

    def test_cap_hard_cuts_single_oversize_unit(self):
        """A single unit larger than the whole budget is hard-cut to fit, with the marker;
        the block stays within the char-equivalent budget (R5)."""
        from fleet_engine.engine import (
            _cap_stage_text, CHAIN_STAGE_TOKEN_CAP, CHARS_PER_TOKEN, _CHAIN_TRUNC_MARKER)

        char_budget = CHAIN_STAGE_TOKEN_CAP * CHARS_PER_TOKEN
        text = "X" * (char_budget + 5000)       # one unit, no blank lines
        capped, truncated = _cap_stage_text(text)

        self.assertTrue(truncated)
        self.assertTrue(capped.endswith(_CHAIN_TRUNC_MARKER))
        self.assertEqual(len(capped), char_budget)     # marker room reserved: block == budget
        self.assertEqual(capped.count("X"), char_budget - len(_CHAIN_TRUNC_MARKER))

    def test_cap_passes_through_within_budget(self):
        """Text within the token budget is returned whole, no marker, not truncated."""
        from fleet_engine.engine import _cap_stage_text

        text = "finding one\n\nfinding two\n\nfinding three"
        self.assertEqual(_cap_stage_text(text), (text, False))

    def test_cap_threads_8kb_multifinding_input_whole(self):
        """R2/R3: an ~8 KB, 10-unit input (the #17 dogfood shape) now threads WHOLE — the
        old 4,000-CHAR cap truncated it to ~4 findings; the 4,000-TOKEN budget does not."""
        from fleet_engine.engine import _cap_stage_text

        text = "\n\n".join(f"Finding {i}: " + "z" * 780 for i in range(10))   # ~8 KB
        capped, truncated = _cap_stage_text(text)
        self.assertFalse(truncated)
        self.assertEqual(capped, text)
        self.assertIn("Finding 9", capped)     # the 10th finding survives (was cut at 4k chars)

    def test_estimate_tokens_floors_chars_over_ratio(self):
        # 43 chars / 4 = 10.75 -> 10 (floor); a hardcoded expected pins FLOOR
        # semantics (round/ceil would give 11) and catches a ratio/operator drift.
        from fleet_engine.engine import _estimate_tokens
        self.assertEqual(_estimate_tokens("x" * 43), 10)

    def test_cap_bound_is_char_based_not_token_density_aware(self):
        """The cap is a CHAR bound (density-blind): dense content (CJK/code, where the
        ~4 chars/token estimate under-counts) is bounded by CHARS, not real tokens — a
        documented limitation of the provider-neutral heuristic (#39, Codex fold)."""
        from fleet_engine.engine import _cap_stage_text, CHAIN_STAGE_TOKEN_CAP, CHARS_PER_TOKEN
        char_budget = CHAIN_STAGE_TOKEN_CAP * CHARS_PER_TOKEN
        # Over the CHAR bound -> truncated + flagged, regardless of the (higher) real token count.
        dense_over = "中" * (char_budget + 100)
        capped, truncated = _cap_stage_text(dense_over)
        self.assertTrue(truncated)
        self.assertLessEqual(len(capped), char_budget)
        # Under the CHAR bound -> passes whole, even though its real token count is high.
        dense_under = "中" * (char_budget // 2)
        self.assertEqual(_cap_stage_text(dense_under), (dense_under, False))

    # ------------------------------------------------------------------
    # Status precedence: broken sequential+collect chain is NOT SUCCESS
    # ------------------------------------------------------------------

    def test_status_precedence_broken_collect_is_degraded_not_success(self):
        """A sequential+collect chain that breaks mid-way → DEGRADED, NOT SUCCESS.

        The parallel-collect unconditional-SUCCESS rule does NOT apply to sequential
        chains: surviving lanes do not guarantee the terminal lane completed.
        """
        client = FakeClient({"analyst": ("fail", "e")})
        result = run_fleet(_chain_config(), "task", client)
        self.assertNotEqual(result.status, FleetStatus.SUCCESS)
        self.assertIs(result.status, FleetStatus.DEGRADED)

    # ------------------------------------------------------------------
    # Parallel path is untouched — a parallel run returns topology="parallel"
    # ------------------------------------------------------------------

    def test_parallel_topology_unchanged(self):
        """A parallel fleet returns topology='parallel'; the chain code is never entered."""
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        result = run_fleet(_config(), "task", client)
        self.assertEqual(result.topology, "parallel")
        self.assertIsNone(result.terminal_produced)  # None = not a chain


class TestU2IterativeCarriers(unittest.TestCase):
    """U2 additive fields: rounds and diversity_collapsed on FleetResult.

    Purely additive — no population logic (U3b populates these).
    All existing parallel/sequential paths must be unaffected (the full suite guards this).
    """

    # ------------------------------------------------------------------
    # Parallel run — both fields must carry their defaults
    # ------------------------------------------------------------------

    def test_parallel_run_rounds_is_none(self):
        """A parallel fleet run carries rounds=None (not iterative)."""
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        result = run_fleet(_config(), "task", client)
        self.assertIsNone(result.rounds)

    def test_parallel_run_diversity_collapsed_is_false(self):
        """A parallel fleet run carries diversity_collapsed=False (not iterative)."""
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        result = run_fleet(_config(), "task", client)
        self.assertFalse(result.diversity_collapsed)

    # ------------------------------------------------------------------
    # Sequential run — both fields must carry their defaults
    # ------------------------------------------------------------------

    def test_sequential_run_rounds_is_none(self):
        """A sequential fleet run carries rounds=None (not iterative)."""
        client = FakeClient()
        result = run_fleet(_chain_config(), "task", client)
        self.assertIsNone(result.rounds)

    def test_sequential_run_diversity_collapsed_is_false(self):
        """A sequential fleet run carries diversity_collapsed=False (not iterative)."""
        client = FakeClient()
        result = run_fleet(_chain_config(), "task", client)
        self.assertFalse(result.diversity_collapsed)

    # ------------------------------------------------------------------
    # Direct construction — defaults when kwargs are absent
    # ------------------------------------------------------------------

    def test_rounds_defaults_to_none_on_construction(self):
        """FleetResult constructed without rounds= has rounds=None."""
        result = FleetResult(fleet="f", task="t", specialists=[], status=FleetStatus.FAILED)
        self.assertIsNone(result.rounds)

    def test_diversity_collapsed_defaults_to_false_on_construction(self):
        """FleetResult constructed without diversity_collapsed= has diversity_collapsed=False."""
        result = FleetResult(fleet="f", task="t", specialists=[], status=FleetStatus.FAILED)
        self.assertFalse(result.diversity_collapsed)

    # ------------------------------------------------------------------
    # Direct construction — both fields are settable (U3b will set them)
    # ------------------------------------------------------------------

    def test_rounds_settable_via_construction(self):
        """rounds=[[...]] can be set via direct construction and reads back correctly."""
        r = AgentResult(role="r1", provider="p", model="m", ok=True, text="out")
        result = FleetResult(
            fleet="f", task="t", specialists=[],
            status=FleetStatus.SUCCESS,
            rounds=[[r]],
        )
        self.assertIsNotNone(result.rounds)
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0][0].role, "r1")

    def test_diversity_collapsed_settable_via_construction(self):
        """diversity_collapsed=True can be set via direct construction and reads back correctly."""
        result = FleetResult(
            fleet="f", task="t", specialists=[],
            status=FleetStatus.DEGRADED,
            diversity_collapsed=True,
        )
        self.assertTrue(result.diversity_collapsed)

    def test_both_iterative_fields_settable_together(self):
        """rounds and diversity_collapsed can both be set in one construction."""
        r = AgentResult(role="a", provider="p", model="m", ok=True, text="out")
        result = FleetResult(
            fleet="f", task="t", specialists=[],
            status=FleetStatus.SUCCESS,
            rounds=[[r], [r]],
            diversity_collapsed=True,
        )
        self.assertEqual(len(result.rounds), 2)
        self.assertTrue(result.diversity_collapsed)


class TestRunRoundDirect(unittest.TestCase):
    """Call _run_round directly with an arbitrary lane subset — proves the helper works
    independently of _fan_out and returns results keyed on the passed lanes list, not
    the full config order. This is the contract iterative rounds depend on.
    """

    def test_subset_returns_in_lanes_order(self):
        """_run_round with 2-of-3 specialists returns results in the passed lanes order."""
        from fleet_engine.engine import _run_round, _specialist_call

        cfg = _config()
        # Use only the first 2 (web, social) — a survivor-subset, just like iterative needs.
        subset = cfg.specialists[:2]

        client = FakeClient()
        events: list = []

        results = _run_round(
            subset,
            lambda spec: _specialist_call(client, spec, "round task"),
            call_timeout=2.0,
            progress=lambda e: events.append(e),
        )

        # Exactly 2 results, in LANES order (web first, then social).
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].role, "web")
        self.assertEqual(results[1].role, "social")

        # Both completed successfully (FakeClient defaults to ok for unknown roles).
        self.assertTrue(results[0].ok)
        self.assertTrue(results[1].ok)
        self.assertFalse(results[0].timed_out)
        self.assertFalse(results[1].timed_out)

        # Toolset is stamped from the spec (not left as the client's echo).
        self.assertEqual(results[0].toolset, ["web"])
        self.assertEqual(results[1].toolset, ["x_search"])

        # LaneLaunched emitted exactly once, listing only the subset's roles.
        launched = [e for e in events if isinstance(e, LaneLaunched)]
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0].roles, ["web", "social"])

        # One LaneDone per lane (2 total, not 3).
        done = [e for e in events if isinstance(e, LaneDone)]
        self.assertEqual(len(done), 2)


def _iterative_config(**overrides):
    """Build an iterative FleetConfig (default: 3 lanes, 3 rounds, convergence=synthesize)."""
    data = {
        "name": "t",
        "topology": "iterative",
        "rounds": 3,
        "convergence": "synthesize",
        "synthesis": {"provider": "openrouter", "model": "synth/m", "prompt": "SYNTH:"},
        "specialists": [
            {"role": "alpha", "provider": "openrouter", "model": "a/m",
             "toolset": [], "focus": "analyze alpha"},
            {"role": "beta", "provider": "xai", "model": "b/m",
             "toolset": [], "focus": "analyze beta"},
            {"role": "gamma", "provider": "openrouter", "model": "c/m",
             "toolset": [], "focus": "analyze gamma"},
        ],
    }
    data.update(overrides)
    cfg = FleetConfig.from_dict(data)
    resolve(cfg, "/unused")
    return cfg


class RoundAwareFakeClient:
    """FakeClient whose behavior is keyed by (role, round_number).

    Tracks a per-role call counter (incremented atomically within a round — rounds
    are serial, and within a round each role is called exactly once, so no two
    threads race on the same role's counter). The call count for a role IS its round
    number. Records prompts per (role, round) for threading assertions.

    schedule: {(role, round_num): ('fail', 'error_msg')} — everything else defaults
    to ('ok', '{role}-r{round}-output').
    """

    def __init__(self, schedule=None):
        self.schedule = schedule or {}
        self._lock = threading.Lock()
        self._call_count: dict[str, int] = {}   # role -> how many times called so far
        self.prompts: dict[tuple[str, int], str] = {}   # (role, round) -> prompt
        self.toolset_for: dict[tuple[str, int], list] = {}   # (role, round) -> toolset

    def run(self, *, role, provider, model, prompt, toolset=()):
        with self._lock:
            count = self._call_count.get(role, 0) + 1
            self._call_count[role] = count
            # Record under the lock too: run() is called concurrently by the per-round
            # worker threads, so these dict writes must not race (Copilot review).
            self.prompts[(role, count)] = prompt
            self.toolset_for[(role, count)] = list(toolset)

        kind, payload = self.schedule.get((role, count), ("ok", f"{role}-r{count}-output"))
        ok = kind == "ok"
        return AgentResult(
            role=role, provider=provider, model=model, ok=ok,
            text=payload if ok else None, error=None if ok else payload,
        )

    def prompt_for(self, role, round_num):
        return self.prompts[(role, round_num)]


class TestIterativeTopology(unittest.TestCase):
    """_run_iterate: bounded-round concurrent fan-out with inter-round threading."""

    # ------------------------------------------------------------------
    # AE1: full debate — 3 lanes, 3 rounds, all ok, convergence synthesize
    # ------------------------------------------------------------------

    def test_ae1_full_debate_status_success(self):
        """All lanes survive all 3 rounds → SUCCESS; synthesis runs over round-3 survivors."""
        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "SYNTH OUT")})
        result = run_fleet(_iterative_config(), "task", client)

        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertTrue(result.ok)
        self.assertEqual(result.topology, "iterative")

    def test_run_fleet_rejects_iterative_rounds_below_one(self):
        """Defense-in-depth: an iterative config with rounds < 1 — only reachable by a
        direct construction that bypasses config validation — fails loud, not a silent
        FAILED (CodeRabbit review)."""
        cfg = _iterative_config()
        cfg.rounds = 0  # bypass config validation via direct field mutation
        with self.assertRaises(ValueError):
            run_fleet(cfg, "task", RoundAwareFakeClient())

    def test_ae1_full_debate_three_rounds_captured(self):
        """3 rounds executed → rounds transcript has 3 entries, each with 3 lane results."""
        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "SYNTH")})
        result = run_fleet(_iterative_config(), "task", client)

        self.assertIsNotNone(result.rounds)
        self.assertEqual(len(result.rounds), 3)
        for i, round_list in enumerate(result.rounds):
            self.assertEqual(len(round_list), 3, f"round {i + 1} should have 3 results")

    def test_ae1_round2_prompt_contains_previous_round_outputs(self):
        """Round-2 prompts contain the DATA marker + round-1 outputs for all lanes."""
        from fleet_engine.engine import _CHAIN_DELIM

        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "S")})
        run_fleet(_iterative_config(), "task", client)

        # Round-2 each lane should see all round-1 outputs via the framing block.
        for role in ("alpha", "beta", "gamma"):
            prompt = client.prompt_for(role, 2)
            self.assertIn("Prior chain stages", prompt, f"{role} round-2 prompt missing DATA framing")
            # Should contain all other round-1 outputs
            for other_role in ("alpha", "beta", "gamma"):
                self.assertIn(
                    _CHAIN_DELIM.format(role=other_role), prompt,
                    f"{role} round-2 prompt missing delimiter for {other_role}",
                )
                self.assertIn(
                    f"{other_role}-r1-output", prompt,
                    f"{role} round-2 prompt missing round-1 output from {other_role}",
                )

    def test_ae1_round3_prompt_contains_round2_outputs(self):
        """Round-3 prompts contain round-2 outputs (previous-round visibility, not full history)."""
        from fleet_engine.engine import _CHAIN_DELIM

        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "S")})
        run_fleet(_iterative_config(), "task", client)

        # Round-3: each lane sees round-2 outputs only.
        for role in ("alpha", "beta", "gamma"):
            prompt = client.prompt_for(role, 3)
            self.assertIn("Prior chain stages", prompt)
            for other_role in ("alpha", "beta", "gamma"):
                self.assertIn(f"{other_role}-r2-output", prompt,
                              f"{role} round-3 prompt missing round-2 output from {other_role}")
            # Round-1 outputs should NOT appear (previous-round only, not full history).
            for other_role in ("alpha", "beta", "gamma"):
                self.assertNotIn(f"{other_role}-r1-output", prompt,
                                 f"{role} round-3 prompt should not contain round-1 output (prev-round only)")

    def test_ae1_diversity_collapsed_false_on_full_debate(self):
        """A full 3-lane, 3-round debate → diversity_collapsed is False."""
        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "S")})
        result = run_fleet(_iterative_config(), "task", client)
        self.assertFalse(result.diversity_collapsed)

    def test_ae1_round1_prompt_has_no_prior_stages(self):
        """Round-1 lanes get only the base specialist prompt — no prior stages block."""
        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "S")})
        run_fleet(_iterative_config(), "task", client)

        for role in ("alpha", "beta", "gamma"):
            prompt = client.prompt_for(role, 1)
            self.assertNotIn("Prior chain stages", prompt)
            self.assertNotIn("UPSTREAM STAGE", prompt)

    def test_ae1_round_started_events_emitted(self):
        """RoundStarted events are emitted for each round (1 per round)."""
        events = []
        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "S")})
        run_fleet(_iterative_config(), "task", client, progress=lambda e: events.append(e))

        started = [e for e in events if isinstance(e, RoundStarted)]
        self.assertEqual(len(started), 3)
        for i, ev in enumerate(started):
            self.assertEqual(ev.round, i + 1)
            self.assertEqual(ev.total, 3)

    # ------------------------------------------------------------------
    # AE2: mid-run lane failure — X fails round 2; rounds 2 & 3 continue without X
    # ------------------------------------------------------------------

    def test_ae2_dropped_lane_absent_from_later_rounds(self):
        """Lane that fails round 2 is absent from round 3 results; transcript has its R2 failure."""
        # alpha fails in round 2; beta and gamma survive all 3 rounds.
        schedule = {("alpha", 2): ("fail", "auth error"), ("synthesizer", 1): ("ok", "S")}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        self.assertIsNotNone(result.rounds)
        # Round 1: all 3 roles.
        self.assertEqual(len(result.rounds[0]), 3)
        # Round 2: all 3 roles attempted (alpha fails in round 2, not dropped YET).
        self.assertEqual(len(result.rounds[1]), 3)
        # Round 3: only 2 roles (alpha was dropped after round 2).
        self.assertEqual(len(result.rounds[2]), 2)
        round3_roles = {r.role for r in result.rounds[2]}
        self.assertNotIn("alpha", round3_roles)
        self.assertIn("beta", round3_roles)
        self.assertIn("gamma", round3_roles)

    def test_ae2_dropped_lane_in_failures(self):
        """Dropped lane appears in result.failures (its drop-round failure is its representative)."""
        schedule = {("alpha", 2): ("fail", "auth error"), ("synthesizer", 1): ("ok", "S")}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        failure_roles = [r.role for r in result.failures]
        self.assertIn("alpha", failure_roles, "dropped lane must appear in failures")
        self.assertNotIn("beta", failure_roles)
        self.assertNotIn("gamma", failure_roles)

    def test_ae2_convergence_runs_over_round3_survivors(self):
        """Convergence runs over the 2 round-3 survivors only (not the dropped alpha)."""
        schedule = {("alpha", 2): ("fail", "auth error"), ("synthesizer", 1): ("ok", "FINAL")}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertEqual(result.synthesis, "FINAL")
        # The synthesizer prompt should not include alpha's output.
        synth_prompt = client.prompt_for("synthesizer", 1)
        self.assertNotIn("alpha", synth_prompt,
                         "dropped alpha must not appear in the synthesizer's survivors block")
        self.assertIn("beta", synth_prompt)
        self.assertIn("gamma", synth_prompt)
        # KTD3: representatives are R*=round-3 outputs, NOT round-1 stale outputs.
        by_role = {r.role: r for r in result.successes}
        self.assertEqual(by_role["beta"].text, "beta-r3-output",
                         "R*=3: beta representative must be round-3 output, not round-1")
        self.assertNotIn("beta-r1-output", synth_prompt,
                         "stale round-1 output must not appear in the synthesizer prompt")

    def test_ae2_two_survivors_in_successes(self):
        """Two surviving lanes in round 3 → result.successes has 2 entries."""
        schedule = {("alpha", 2): ("fail", "e"), ("synthesizer", 1): ("ok", "S")}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)
        self.assertEqual(len(result.successes), 2)

    # ------------------------------------------------------------------
    # AE3: all lanes fail round 1 → FAILED, convergence never runs
    # ------------------------------------------------------------------

    def test_ae3_all_fail_round1_status_failed(self):
        """All lanes fail round 1 → FAILED; convergence never called."""
        schedule = {
            ("alpha", 1): ("fail", "down"),
            ("beta", 1): ("fail", "down"),
            ("gamma", 1): ("fail", "down"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        self.assertIs(result.status, FleetStatus.FAILED)
        self.assertFalse(result.ok)

    def test_ae3_all_fail_round1_convergence_not_run(self):
        """synthesis=None and synth_ok=None (convergence never invoked on all-fail round 1)."""
        schedule = {
            ("alpha", 1): ("fail", "down"),
            ("beta", 1): ("fail", "down"),
            ("gamma", 1): ("fail", "down"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        self.assertIsNone(result.synthesis)
        self.assertIsNone(result.synth_ok)
        # Synthesizer must not have been called.
        self.assertNotIn(("synthesizer", 1), client.prompts)

    def test_ae3_all_fail_round1_one_round_in_transcript(self):
        """rounds transcript has exactly 1 round when all lanes fail round 1."""
        schedule = {
            ("alpha", 1): ("fail", "down"),
            ("beta", 1): ("fail", "down"),
            ("gamma", 1): ("fail", "down"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)
        self.assertEqual(len(result.rounds), 1)

    def test_ae3_all_fail_round1_failure_note(self):
        """'all specialists failed' note is present on all-fail round-1."""
        schedule = {r: ("fail", "down") for r in [("alpha", 1), ("beta", 1), ("gamma", 1)]}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)
        self.assertTrue(any("all specialists failed" in n for n in result.notes))

    # ------------------------------------------------------------------
    # AE4: self-refine — 1 lane, 3 rounds, all ok
    # ------------------------------------------------------------------

    def test_ae4_self_refine_success(self):
        """1 lane, 3 rounds, all ok → SUCCESS; diversity_collapsed=False (single lane)."""
        data = {
            "name": "t",
            "topology": "iterative",
            "rounds": 3,
            "convergence": "synthesize",
            "synthesis": {"provider": "openrouter", "model": "synth/m"},
            "specialists": [
                {"role": "refiner", "provider": "openrouter", "model": "r/m",
                 "toolset": [], "focus": "refine output"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        schedule = {("synthesizer", 1): ("ok", "REFINED")}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(cfg, "task", client)

        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertFalse(result.diversity_collapsed, "single lane never collapses diversity")
        self.assertEqual(result.failures, [])

    def test_ae4_self_refine_sees_own_prior_output(self):
        """Self-refine: round-2 and round-3 prompts contain the lane's own prior output."""
        from fleet_engine.engine import _CHAIN_DELIM

        data = {
            "name": "t",
            "topology": "iterative",
            "rounds": 3,
            "convergence": "collect",
            "specialists": [
                {"role": "refiner", "provider": "openrouter", "model": "r/m",
                 "toolset": [], "focus": "refine output"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        client = RoundAwareFakeClient()
        run_fleet(cfg, "task", client)

        # Round-2: sees round-1 output.
        prompt_r2 = client.prompt_for("refiner", 2)
        self.assertIn("refiner-r1-output", prompt_r2)
        self.assertIn(_CHAIN_DELIM.format(role="refiner"), prompt_r2)

        # Round-3: sees round-2 output only (previous-round, not full history).
        prompt_r3 = client.prompt_for("refiner", 3)
        self.assertIn("refiner-r2-output", prompt_r3)
        self.assertNotIn("refiner-r1-output", prompt_r3)

    def test_ae4_self_refine_three_rounds_captured(self):
        """3 rounds in the transcript; each has exactly 1 lane result."""
        data = {
            "name": "t",
            "topology": "iterative",
            "rounds": 3,
            "convergence": "collect",
            "specialists": [
                {"role": "refiner", "provider": "openrouter", "model": "r/m",
                 "toolset": [], "focus": "refine"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        client = RoundAwareFakeClient()
        result = run_fleet(cfg, "task", client)

        self.assertEqual(len(result.rounds), 3)
        for round_list in result.rounds:
            self.assertEqual(len(round_list), 1)

    # ------------------------------------------------------------------
    # AE5: debate + judge — convergence runs over last-surviving round
    # ------------------------------------------------------------------

    def test_ae5_debate_judge_success(self):
        """Debate with convergence=judge → judge runs over round-3 survivors; SUCCESS."""
        data = {
            "name": "t",
            "topology": "iterative",
            "rounds": 3,
            "convergence": "judge",
            "judge": {"provider": "openrouter", "model": "j/m"},
            "specialists": [
                {"role": "alpha", "provider": "openrouter", "model": "a/m",
                 "toolset": [], "focus": "analyze A"},
                {"role": "beta", "provider": "xai", "model": "b/m",
                 "toolset": [], "focus": "analyze B"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        schedule = {("judge", 1): ("ok", "GRADES")}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(cfg, "task", client)

        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertEqual(result.judge, "GRADES")
        self.assertIs(result.judge_ok, True)

    def test_ae5_judge_failure_degrades(self):
        """Judge fails → DEGRADED; final-round outputs preserved in successes."""
        data = {
            "name": "t",
            "topology": "iterative",
            "rounds": 3,
            "convergence": "judge",
            "judge": {"provider": "openrouter", "model": "j/m"},
            "specialists": [
                {"role": "alpha", "provider": "openrouter", "model": "a/m",
                 "toolset": [], "focus": "analyze A"},
                {"role": "beta", "provider": "xai", "model": "b/m",
                 "toolset": [], "focus": "analyze B"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")

        schedule = {("judge", 1): ("fail", "rate limited")}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(cfg, "task", client)

        self.assertIs(result.status, FleetStatus.DEGRADED)
        self.assertIsNone(result.judge)
        self.assertIs(result.judge_ok, False)
        # Final-round outputs must still be accessible as successes.
        self.assertEqual(len(result.successes), 2, "final-round outputs must be preserved")

    # ------------------------------------------------------------------
    # AE6: mid-run collapse — all 3 ok round 1, all fail round 2, 3 rounds configured
    # ------------------------------------------------------------------

    def test_ae6_collapse_early_stop(self):
        """All 3 ok round 1, all fail round 2 → early stop; 2 rounds in transcript."""
        schedule = {
            ("alpha", 2): ("fail", "down"),
            ("beta", 2): ("fail", "down"),
            ("gamma", 2): ("fail", "down"),
        }
        client = RoundAwareFakeClient(schedule=schedule)

        # Use collect so convergence is not the issue.
        cfg = _iterative_config(convergence="collect")
        result = run_fleet(cfg, "task", client)

        # Early stop after round 2 (all dropped).
        self.assertEqual(len(result.rounds), 2)

    def test_ae6_convergence_over_round1(self):
        """After collapse, convergence runs over round-1 survivors (the last surviving round)."""
        schedule = {
            ("alpha", 2): ("fail", "down"),
            ("beta", 2): ("fail", "down"),
            ("gamma", 2): ("fail", "down"),
            ("synthesizer", 1): ("ok", "R1 SYNTH"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        # Synthesis ran over round-1 outputs.
        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertEqual(result.synthesis, "R1 SYNTH")
        # KTD3: R*=1 → synthesizer prompt must contain round-1 outputs, not round-2 failures.
        synth_prompt = client.prompt_for("synthesizer", 1)
        self.assertIn("alpha-r1-output", synth_prompt,
                      "R*=1: synthesizer must see round-1 outputs")

    def test_ae6_failures_empty(self):
        """Round-1 lanes succeeded → representatives are round-1 successes → failures empty."""
        schedule = {
            ("alpha", 2): ("fail", "down"),
            ("beta", 2): ("fail", "down"),
            ("gamma", 2): ("fail", "down"),
            ("synthesizer", 1): ("ok", "S"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        self.assertEqual(result.failures, [],
                         "round-1 successes are the representatives → no failures")

    def test_ae6_diversity_collapsed_true(self):
        """Zero cross-round iterations (R_star==1) on a multi-lane fleet → diversity_collapsed=True."""
        schedule = {
            ("alpha", 2): ("fail", "down"),
            ("beta", 2): ("fail", "down"),
            ("gamma", 2): ("fail", "down"),
            ("synthesizer", 1): ("ok", "S"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)
        self.assertTrue(result.diversity_collapsed)

    def test_ae6_status_success(self):
        """Collapse doesn't change status to DEGRADED — SUCCESS because convergence succeeded."""
        schedule = {
            ("alpha", 2): ("fail", "down"),
            ("beta", 2): ("fail", "down"),
            ("gamma", 2): ("fail", "down"),
            ("synthesizer", 1): ("ok", "S"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)
        self.assertIs(result.status, FleetStatus.SUCCESS)

    # ------------------------------------------------------------------
    # Collapse to a lone survivor (3 lanes, round-3 has only 1 ok)
    # ------------------------------------------------------------------

    def test_lone_survivor_diversity_collapsed_true(self):
        """3 lanes, but only 1 survives to the last round → diversity_collapsed=True."""
        schedule = {
            ("beta", 2): ("fail", "down"),
            ("gamma", 2): ("fail", "down"),
            ("synthesizer", 1): ("ok", "S"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        # Only alpha survives to rounds 3; beta and gamma dropped after round 2.
        # R_star = 3 (alpha survived); n_ok_star = 1 (only alpha in round 3).
        self.assertTrue(result.diversity_collapsed)

    def test_fail_inside_rstar_representatives_are_rstar_records(self):
        """Beta+gamma fail IN the last surviving round (R*=3) — not dropped earlier.

        _representative_results must pick each lane's R* record:
        alpha → R*-round success, beta/gamma → R*-round failure (land in result.failures).
        Convergence runs over alpha alone (the only R*-round success).
        """
        schedule = {
            ("beta", 3): ("fail", "round3-fail"),
            ("gamma", 3): ("fail", "round3-fail"),
            ("synthesizer", 1): ("ok", "ALPHA ONLY"),
        }
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(), "task", client)

        # R*=3, n_ok_star=1 → collapsed
        self.assertTrue(result.diversity_collapsed)
        # Beta and gamma failed IN R* → they appear in result.failures
        failure_roles = {r.role for r in result.failures}
        self.assertIn("beta", failure_roles,
                      "beta failed in R*=3 and must be a representative failure")
        self.assertIn("gamma", failure_roles,
                      "gamma failed in R*=3 and must be a representative failure")
        # Convergence ran over alpha's R* success only
        synth_prompt = client.prompt_for("synthesizer", 1)
        self.assertIn("alpha-r3-output", synth_prompt,
                      "synthesizer must see alpha's round-3 output")
        self.assertNotIn("beta", synth_prompt,
                         "failed beta must not appear in the synthesizer's survivors block")

    # ------------------------------------------------------------------
    # Iterative + collect (never DEGRADED)
    # ------------------------------------------------------------------

    def test_iterative_collect_all_ok_success(self):
        """Iterative + collect, all lanes survive → SUCCESS; synthesis=None."""
        cfg = _iterative_config(convergence="collect")
        client = RoundAwareFakeClient()
        result = run_fleet(cfg, "task", client)

        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertIsNone(result.synthesis)
        self.assertIsNone(result.synth_ok)
        self.assertEqual(result.convergence, "collect")

    def test_iterative_collect_never_degraded(self):
        """Iterative + collect is never DEGRADED — partial failure → still SUCCESS."""
        schedule = {("gamma", 1): ("fail", "down")}
        cfg = _iterative_config(convergence="collect")
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(cfg, "task", client)

        self.assertIsNot(result.status, FleetStatus.DEGRADED)
        self.assertIs(result.status, FleetStatus.SUCCESS)

    def test_iterative_collect_all_fail_is_failed(self):
        """Iterative + collect, all lanes fail round 1 → FAILED (never DEGRADED)."""
        schedule = {
            ("alpha", 1): ("fail", "down"),
            ("beta", 1): ("fail", "down"),
            ("gamma", 1): ("fail", "down"),
        }
        cfg = _iterative_config(convergence="collect")
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(cfg, "task", client)
        self.assertIs(result.status, FleetStatus.FAILED)

    # ------------------------------------------------------------------
    # Toolset: [] iterative lane runs with zero tools (not None)
    # ------------------------------------------------------------------

    def test_toolset_empty_list_not_none(self):
        """An iterative lane with toolset:[] receives toolset=[] (not None) in every round."""
        cfg = _iterative_config(rounds=2)
        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "S")})
        result = run_fleet(cfg, "task", client)

        # Every lane result in every round should have toolset == [].
        for round_list in result.rounds:
            for lane_result in round_list:
                self.assertEqual(lane_result.toolset, [],
                                 f"{lane_result.role} must have toolset=[], not None")
                self.assertIsNotNone(lane_result.toolset)

    # ------------------------------------------------------------------
    # threading_truncated flag — set when inter-round content is capped
    # ------------------------------------------------------------------

    def test_threading_truncated_when_inter_round_output_oversize(self):
        """threading_truncated=True when a round's output exceeds the token budget."""
        from fleet_engine.engine import CHAIN_STAGE_TOKEN_CAP, CHARS_PER_TOKEN

        long_text = "X" * (CHAIN_STAGE_TOKEN_CAP * CHARS_PER_TOKEN + 100)

        class BigOutputClient:
            def __init__(self):
                self.calls: list[tuple[str, str]] = []
                self.toolset_for: dict[str, list] = {}

            def run(self, *, role, provider, model, prompt, toolset=()):
                self.calls.append((role, prompt))
                self.toolset_for[role] = list(toolset)
                if role == "synthesizer":
                    return AgentResult(role=role, provider=provider, model=model,
                                       ok=True, text="S")
                return AgentResult(role=role, provider=provider, model=model,
                                   ok=True, text=long_text)

        client = BigOutputClient()
        cfg = _iterative_config(rounds=2)
        result = run_fleet(cfg, "task", client)
        self.assertTrue(result.threading_truncated)

    def test_no_threading_truncated_when_outputs_small(self):
        """threading_truncated=False when all inter-round outputs are within the cap."""
        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "S")})
        result = run_fleet(_iterative_config(rounds=2), "task", client)
        self.assertFalse(result.threading_truncated)

    # ------------------------------------------------------------------
    # Terminal_produced: None for iterative (not a chain)
    # ------------------------------------------------------------------

    def test_terminal_produced_is_none_for_iterative(self):
        """iterative runs carry terminal_produced=None (not a chain topology)."""
        client = RoundAwareFakeClient(schedule={("synthesizer", 1): ("ok", "S")})
        result = run_fleet(_iterative_config(), "task", client)
        self.assertIsNone(result.terminal_produced)

    # ------------------------------------------------------------------
    # rounds=1 edge case: single-round iterative (no cross-round iterations)
    # → diversity_collapsed=True for multi-lane (R_star==1 condition)
    # ------------------------------------------------------------------

    def test_rounds1_multi_lane_diversity_collapsed(self):
        """rounds:1 multi-lane fleet completes with 0 cross-round iterations → collapsed=True."""
        schedule = {("synthesizer", 1): ("ok", "S")}
        client = RoundAwareFakeClient(schedule=schedule)
        result = run_fleet(_iterative_config(rounds=1), "task", client)

        self.assertIs(result.status, FleetStatus.SUCCESS)
        self.assertTrue(result.diversity_collapsed,
                        "rounds:1 multi-lane fleet has zero cross-round iterations → collapsed")

    # ------------------------------------------------------------------
    # Parallel and sequential paths unaffected by the iterative branch
    # ------------------------------------------------------------------

    def test_parallel_path_untouched_by_iterative_branch(self):
        """A default parallel fleet is unaffected by the iterative branch."""
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        result = run_fleet(_config(), "task", client)
        self.assertEqual(result.topology, "parallel")
        self.assertIsNone(result.rounds)
        self.assertFalse(result.diversity_collapsed)

    def test_sequential_path_untouched_by_iterative_branch(self):
        """A sequential fleet is unaffected by the iterative branch."""
        client = FakeClient()
        result = run_fleet(_chain_config(), "task", client)
        self.assertEqual(result.topology, "sequential")
        self.assertIsNone(result.rounds)
        self.assertFalse(result.diversity_collapsed)


if __name__ == "__main__":
    unittest.main()
