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
from fleet_engine.progress import JudgeDone, JudgeStarted, LaneDone


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


if __name__ == "__main__":
    unittest.main()
