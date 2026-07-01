"""Tests for the progress-event seam (U1).

The engine emits pure lifecycle events through an optional ``progress`` hook;
these tests record the events and assert their shape, ordering, and arrival-order
honesty — with no I/O. Fixtures build FleetConfig + fake clients directly, in the
repo's ``_config`` / fake-client style (mirrors tests/test_engine.py).
"""

import pathlib
import threading
import time
import unittest

import fleet_engine.engine as engine_mod
import fleet_engine.model_client as model_client_mod
from fleet_engine.config import FleetConfig
from fleet_engine.engine import run_fleet
from fleet_engine.model_client import AgentResult
from fleet_engine.personas import resolve
from fleet_engine.progress import (
    JudgeDone,
    JudgeStarted,
    LaneDone,
    LaneLaunched,
    LaneStarted,
    ProgressEvent,
    SynthDone,
    SynthStarted,
    noop,
    outcome_label,
)


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
    """Behavior keyed by role -> ('ok', text) | ('fail', error); returns instantly."""

    def __init__(self, behavior=None):
        self.behavior = behavior or {}

    def run(self, *, role, provider, model, prompt, toolset=()):
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(role=role, provider=provider, model=model, ok=ok,
                           text=payload if ok else None, error=None if ok else payload)


class SleepyClient:
    """Chosen roles sleep ``delay`` seconds before returning — a slow-but-not-hung
    provider, to exercise arrival-order draining and the late-return path."""

    def __init__(self, slow_roles=(), delay=0.0, behavior=None):
        self.slow = set(slow_roles)
        self.delay = delay
        self.behavior = behavior or {}

    def run(self, *, role, provider, model, prompt, toolset=()):
        if role in self.slow:
            time.sleep(self.delay)
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(role=role, provider=provider, model=model, ok=ok,
                           text=payload if ok else None, error=None if ok else payload)


class HangingClient:
    """Chosen roles hang forever (abandoned daemon at exit); others return instantly."""

    def __init__(self, hang_roles=(), behavior=None):
        self.hang = set(hang_roles)
        self.behavior = behavior or {}
        self._never = threading.Event()

    def run(self, *, role, provider, model, prompt, toolset=()):
        if role in self.hang:
            self._never.wait()
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(role=role, provider=provider, model=model, ok=ok,
                           text=payload if ok else None, error=None if ok else payload)


class Recorder:
    """Progress hook that records every event in the order it was emitted."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def of(self, cls):
        return [e for e in self.events if isinstance(e, cls)]


class TestEventSequence(unittest.TestCase):
    """The hook receives launched -> per-lane done -> synth-started -> synth-done."""

    def test_full_lifecycle_order(self):
        # Covers AE1.
        rec = Recorder()
        run_fleet(_config(), "task", FakeClient({"synthesizer": ("ok", "FINAL")}), progress=rec)
        kinds = [type(e).__name__ for e in rec.events]
        self.assertEqual(kinds[0], "LaneLaunched", "launched fires first")
        self.assertEqual(kinds[-2:], ["SynthStarted", "SynthDone"], "synthesis closes the run")
        # Every event between launched and synth-started is a lane-done.
        first_synth = kinds.index("SynthStarted")
        self.assertTrue(all(k == "LaneDone" for k in kinds[1:first_synth]))
        self.assertEqual(len(rec.of(LaneDone)), 3)

    def test_lane_launched_names_all_roles(self):
        rec = Recorder()
        run_fleet(_config(), "task", FakeClient({"synthesizer": ("ok", "F")}), progress=rec)
        self.assertEqual(sorted(rec.of(LaneLaunched)[0].roles), ["analysis", "social", "web"])

    def test_synth_started_carries_survivor_count(self):
        rec = Recorder()
        client = FakeClient({"social": ("fail", "x"), "synthesizer": ("ok", "F")})
        run_fleet(_config(), "task", client, progress=rec)
        self.assertEqual(rec.of(SynthStarted)[0].survivors, 2)

    def test_synth_done_outcome_ok(self):
        rec = Recorder()
        run_fleet(_config(), "task", FakeClient({"synthesizer": ("ok", "F")}), progress=rec)
        self.assertEqual(rec.of(SynthDone)[0].outcome, "ok")
        self.assertIsNotNone(rec.of(SynthDone)[0].elapsed_s)

    def test_synth_done_outcome_failed(self):
        rec = Recorder()
        run_fleet(_config(), "task", FakeClient({"synthesizer": ("fail", "rate limited")}), progress=rec)
        self.assertEqual(rec.of(SynthDone)[0].outcome, "failed")

    def test_synth_done_outcome_timed_out(self):
        # The third outcome_label value: a wedged synthesizer reports "timed-out",
        # distinct from a plain "failed" (exercises the full label set through the hook).
        rec = Recorder()
        client = HangingClient(hang_roles={"synthesizer"})
        run_fleet(_config(), "task", client, call_timeout=0.3, progress=rec)
        self.assertEqual(rec.of(SynthDone)[0].outcome, "timed-out")


class TestArrivalOrder(unittest.TestCase):
    """A slow lane never hides a fast one — lane-done is drained in arrival order."""

    def test_slow_early_lane_does_not_hide_fast_later_lanes(self):
        # Covers AE1. web is config index 0 AND slow; list-order collection would
        # emit it first (blocking the others). Arrival order emits it LAST.
        rec = Recorder()
        client = SleepyClient(slow_roles={"web"}, delay=0.3,
                              behavior={"synthesizer": ("ok", "F")})
        run_fleet(_config(), "task", client, progress=rec)
        done_roles = [e.result.role for e in rec.of(LaneDone)]
        self.assertEqual(len(done_roles), 3)
        self.assertEqual(done_roles[-1], "web", "slow config-index-0 lane must arrive LAST")
        self.assertEqual(set(done_roles[:2]), {"social", "analysis"})


class TestOutcomes(unittest.TestCase):
    def test_failing_lane_emits_failed_outcome(self):
        # Covers AE2.
        rec = Recorder()
        client = FakeClient({"social": ("fail", "auth error"), "synthesizer": ("ok", "F")})
        run_fleet(_config(), "task", client, progress=rec)
        social = next(e for e in rec.of(LaneDone) if e.result.role == "social")
        self.assertEqual(outcome_label(social.result), "failed")
        self.assertFalse(social.result.ok)
        self.assertFalse(social.result.timed_out)

    def test_lane_done_carries_full_result_for_capturer(self):
        # The EVENT carries the whole AgentResult (the capturer writes the .md from
        # it). The no-raw-error-string guarantee is the RENDERER's job (U2), not the
        # event's — so provider/model/toolset/elapsed are all present and stamped.
        rec = Recorder()
        run_fleet(_config(), "task", FakeClient({"synthesizer": ("ok", "F")}), progress=rec)
        web = next(e for e in rec.of(LaneDone) if e.result.role == "web")
        r = web.result
        self.assertEqual(r.provider, "openrouter")
        self.assertEqual(r.model, "web/model")
        self.assertEqual(r.toolset, ["web"], "toolset stamped before emit")
        self.assertIsNotNone(r.elapsed_s, "elapsed stamped before emit")
        self.assertGreaterEqual(r.elapsed_s, 0.0)
        self.assertEqual(r.text, "web-output")


class TestTimeoutEmission(unittest.TestCase):
    """A wedged lane's timed-out lane-done is emitted by the drainer, exactly once."""

    def test_wedged_lane_timed_out_done_emitted_by_collector(self):
        # Covers AE3.
        rec = Recorder()
        client = HangingClient(hang_roles={"social"}, behavior={"synthesizer": ("ok", "DONE")})
        run_fleet(_config(), "task", client, call_timeout=0.3, progress=rec)
        done = rec.of(LaneDone)
        self.assertEqual(len(done), 3, "one lane-done per lane, exactly")
        social = [e for e in done if e.result.role == "social"]
        self.assertEqual(len(social), 1, "exactly one lane-done for the wedged lane")
        self.assertTrue(social[0].result.timed_out)
        self.assertEqual(outcome_label(social[0].result), "timed-out")

    def test_late_returning_lane_does_not_re_emit(self):
        # A lane that returns AFTER the deadline put()s to the queue, but the drainer
        # never reads it again — so no second lane-done fires.
        rec = Recorder()
        client = SleepyClient(slow_roles={"social"}, delay=0.3,
                              behavior={"synthesizer": ("ok", "DONE")})
        run_fleet(_config(), "task", client, call_timeout=0.1, progress=rec)
        self.assertEqual(len(rec.of(LaneDone)), 3)
        time.sleep(0.5)  # let the abandoned lane finish (~0.3s) and put() to the queue
        self.assertEqual(len(rec.of(LaneDone)), 3, "no late re-emit after the deadline")
        social = next(e for e in rec.of(LaneDone) if e.result.role == "social")
        self.assertTrue(social.result.timed_out)


class TestSlowHookDoesNotCauseFalseTimeouts(unittest.TestCase):
    """Timeout classification must be independent of progress-hook latency.

    The production hook does stderr rendering AND synchronous per-lane capture I/O.
    Before the fix, a hook slower than call_timeout let the deadline lapse while
    already-finished results sat unread in the queue, fabricating those completed
    lanes as false timeouts (cross-model adversarial finding). Now a lane is a
    timeout iff it never pushed — the drainer drains the backlog before fabricating.
    """

    def test_slow_hook_does_not_time_out_completed_lanes(self):
        def slow_hook(event):
            if isinstance(event, LaneDone):
                time.sleep(0.25)  # > call_timeout; models stalled capture I/O

        # All three specialists return instantly; the hook (not the model) is slow.
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        result = run_fleet(_config(), "task", client, call_timeout=0.1, progress=slow_hook)

        for lane in result.specialists:
            self.assertFalse(lane.timed_out, f"{lane.role} completed but was marked timed_out")
            self.assertTrue(lane.ok, f"{lane.role} should be a success")
        self.assertEqual(len(result.successes), 3)
        self.assertTrue(result.ok)
        # elapsed is measured from the worker's completion, so it is NOT inflated to
        # the ~0.25s hook delay that elapsed between completion and the drainer's pull.
        for lane in result.specialists:
            self.assertLess(lane.elapsed_s, 0.2, f"{lane.role} elapsed inflated by hook latency")


class TestNoSynthOnTotalFailure(unittest.TestCase):
    def test_all_lanes_fail_no_synth_started(self):
        # Covers AE4: every lane fails -> no SynthStarted/SynthDone, no synthesis.
        rec = Recorder()
        client = FakeClient({r: ("fail", "down") for r in ("web", "social", "analysis")})
        result = run_fleet(_config(), "task", client, progress=rec)
        self.assertEqual(len(rec.of(LaneDone)), 3)
        self.assertEqual(rec.of(SynthStarted), [])
        self.assertEqual(rec.of(SynthDone), [])
        self.assertIsNone(result.synthesis)


class TestDefaultNoop(unittest.TestCase):
    def test_omitting_progress_runs_unchanged(self):
        # No progress hook -> default no-op; result identical in shape.
        result = run_fleet(_config(), "task", FakeClient({"synthesizer": ("ok", "F")}))
        self.assertTrue(result.ok)
        self.assertEqual(result.synthesis, "F")
        self.assertEqual(len(result.specialists), 3)

    def test_noop_returns_none(self):
        self.assertIsNone(noop(LaneLaunched(roles=["x"])))


class TestJudgeProgressEvents(unittest.TestCase):
    """JudgeStarted and JudgeDone are valid ProgressEvent members with correct fields."""

    def test_judge_started_is_dataclass_with_survivors(self):
        ev = JudgeStarted(survivors=3)
        self.assertEqual(ev.survivors, 3)

    def test_judge_done_is_dataclass_with_outcome_and_elapsed(self):
        ev = JudgeDone(outcome="ok", elapsed_s=1.5)
        self.assertEqual(ev.outcome, "ok")
        self.assertEqual(ev.elapsed_s, 1.5)

    def test_judge_events_are_frozen(self):
        ev = JudgeStarted(survivors=2)
        with self.assertRaises((AttributeError, TypeError)):
            ev.survivors = 99  # type: ignore[misc]

    def test_judge_started_is_progress_event(self):
        """JudgeStarted is part of the ProgressEvent union (type-system membership)."""
        # The Union type args are the source of truth; check both are present.
        import typing
        args = typing.get_args(ProgressEvent)
        self.assertIn(JudgeStarted, args, "JudgeStarted must be in ProgressEvent union")
        self.assertIn(JudgeDone, args, "JudgeDone must be in ProgressEvent union")

    def test_judge_events_emitted_on_judge_run(self):
        """Engine emits JudgeStarted then JudgeDone on a successful judge fleet run."""
        from fleet_engine.config import FleetConfig
        from fleet_engine.engine import run_fleet
        from fleet_engine.personas import resolve

        cfg = FleetConfig.from_dict({
            "name": "t",
            "convergence": "judge",
            "judge": {"provider": "openrouter", "model": "judge/model"},
            "specialists": [
                {"role": "web", "provider": "openrouter", "model": "web/model",
                 "toolset": ["web"], "focus": "web research"},
            ],
        })
        resolve(cfg, "/unused")

        events = []
        run_fleet(cfg, "task", FakeClient(), progress=lambda e: events.append(e))

        started = [e for e in events if isinstance(e, JudgeStarted)]
        done = [e for e in events if isinstance(e, JudgeDone)]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(done), 1)
        self.assertEqual(started[0].survivors, 1)
        self.assertEqual(done[0].outcome, "ok")

    def test_judge_events_not_emitted_on_synthesize_run(self):
        """A synthesize fleet emits no JudgeStarted/JudgeDone events."""
        from fleet_engine.config import FleetConfig
        from fleet_engine.engine import run_fleet
        from fleet_engine.personas import resolve

        cfg = FleetConfig.from_dict({
            "name": "t",
            "synthesis": {"provider": "openrouter", "model": "s/m"},
            "specialists": [
                {"role": "web", "provider": "openrouter", "model": "m",
                 "toolset": ["web"], "focus": "web research"},
            ],
        })
        resolve(cfg, "/unused")

        events = []
        run_fleet(cfg, "task", FakeClient({"synthesizer": ("ok", "S")}),
                  progress=lambda e: events.append(e))

        self.assertEqual([e for e in events if isinstance(e, JudgeStarted)], [])
        self.assertEqual([e for e in events if isinstance(e, JudgeDone)], [])


class TestEnginePurity(unittest.TestCase):
    """engine.py and model_client.py perform no I/O — the hermetic-suite guard.

    Verified mechanically per the side-effects-at-the-edge learning: grepping the
    engine core for output sinks must find nothing (docstrings discuss I/O in prose,
    but never the call forms below).
    """

    def test_engine_core_has_no_output_sinks(self):
        for mod in (engine_mod, model_client_mod):
            src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
            for forbidden in ("print(", "open(", "sys.stdout", "sys.stderr", "persona", "Path("):
                self.assertNotIn(
                    forbidden, src, f"{mod.__name__} must not contain {forbidden!r}"
                )


# ---------------------------------------------------------------------------
# U4 (fleet-shapes): Validated breadcrumb synthesizer count — collect vs synthesize
# ---------------------------------------------------------------------------


class TestValidatedBreadcrumbSynthesizerCount(unittest.TestCase):
    """run_with_progress emits Validated with synthesizers=0 for collect, 1 for synthesize."""

    def _run_and_get_progress(self, cfg):
        """Run run_with_progress with a FakeClient and capture the progress stream."""
        from io import StringIO
        from fleet_engine.progress_runner import run_with_progress
        prog = StringIO()
        run_with_progress(cfg, "test task", FakeClient({"synthesizer": ("ok", "S")}),
                          run_dir=None, progress_stream=prog)
        return prog.getvalue()

    def _collect_config(self):
        cfg = FleetConfig.from_dict({
            "name": "collect-fleet",
            "convergence": "collect",
            "specialists": [
                {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"],
                 "focus": "web research"},
            ],
        })
        resolve(cfg, "/unused")
        return cfg

    def _synthesize_config(self):
        cfg = FleetConfig.from_dict({
            "name": "synth-fleet",
            "convergence": "synthesize",
            "synthesis": {"provider": "openrouter", "model": "synth/model"},
            "specialists": [
                {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"],
                 "focus": "web research"},
            ],
        })
        resolve(cfg, "/unused")
        return cfg

    def test_collect_validated_breadcrumb_reports_zero_synthesizers(self):
        """A collect fleet's Validated breadcrumb reports 0 synthesizer(s)."""
        text = self._run_and_get_progress(self._collect_config())
        self.assertIn("0 synthesizer(s)", text)

    def test_synthesize_validated_breadcrumb_reports_one_synthesizer(self):
        """A synthesize fleet's Validated breadcrumb still reports 1 synthesizer(s)."""
        text = self._run_and_get_progress(self._synthesize_config())
        self.assertIn("1 synthesizer(s)", text)

    def test_collect_validated_event_synthesizers_is_zero(self):
        """run_with_progress emits a Validated event with synthesizers=0 for collect."""
        from fleet_engine.progress import Validated
        from fleet_engine.progress_runner import run_with_progress

        # Patch ProgressRenderer to capture the Validated event directly.
        # We drive run_with_progress and intercept the emitted Validated events.
        from unittest.mock import patch as _patch

        validated_events = []

        class CapturingRenderer:
            def __init__(self, *args, **kwargs):
                pass

            def emit(self, event):
                if isinstance(event, Validated):
                    validated_events.append(event)

            def start_heartbeat(self):
                pass

            def stop_heartbeat(self):
                pass

            def note(self, _msg):
                pass

        with _patch("fleet_engine.progress_runner.ProgressRenderer", CapturingRenderer):
            run_with_progress(self._collect_config(), "task", FakeClient(),
                              run_dir=None, progress_stream=None)

        self.assertTrue(validated_events, "no Validated event was emitted")
        self.assertEqual(validated_events[0].synthesizers, 0)

    def test_synthesize_validated_event_synthesizers_is_one(self):
        """run_with_progress emits a Validated event with synthesizers=1 for synthesize."""
        from fleet_engine.progress import Validated
        from fleet_engine.progress_runner import run_with_progress
        from unittest.mock import patch as _patch

        validated_events = []

        class CapturingRenderer:
            def __init__(self, *args, **kwargs):
                pass

            def emit(self, event):
                if isinstance(event, Validated):
                    validated_events.append(event)

            def start_heartbeat(self):
                pass

            def stop_heartbeat(self):
                pass

            def note(self, _msg):
                pass

        with _patch("fleet_engine.progress_runner.ProgressRenderer", CapturingRenderer):
            run_with_progress(self._synthesize_config(), "task",
                              FakeClient({"synthesizer": ("ok", "S")}),
                              run_dir=None, progress_stream=None)

        self.assertTrue(validated_events, "no Validated event was emitted")
        self.assertEqual(validated_events[0].synthesizers, 1)

    def _judge_config(self):
        cfg = FleetConfig.from_dict({
            "name": "judge-fleet",
            "convergence": "judge",
            "judge": {"provider": "openrouter", "model": "judge/model"},
            "specialists": [
                {"role": "web", "provider": "openrouter", "model": "m", "toolset": ["web"],
                 "focus": "web research"},
            ],
        })
        resolve(cfg, "/unused")
        return cfg

    def test_judge_validated_breadcrumb_reports_judge(self):
        """A judge fleet's Validated breadcrumb reports '1 judge', not 'synthesizer(s)'."""
        text = self._run_and_get_progress(
            self._judge_config(),
        )
        self.assertIn("1 judge", text)
        self.assertNotIn("synthesizer", text.split("\n")[0] if text else "")

    def test_judge_validated_event_convergence_is_judge(self):
        """run_with_progress emits a Validated event with convergence='judge' for judge fleets."""
        from fleet_engine.progress import Validated
        from fleet_engine.progress_runner import run_with_progress
        from unittest.mock import patch as _patch

        validated_events = []

        class CapturingRenderer:
            def __init__(self, *args, **kwargs):
                pass

            def emit(self, event):
                if isinstance(event, Validated):
                    validated_events.append(event)

            def start_heartbeat(self):
                pass

            def stop_heartbeat(self):
                pass

            def note(self, _msg):
                pass

        with _patch("fleet_engine.progress_runner.ProgressRenderer", CapturingRenderer):
            run_with_progress(
                self._judge_config(), "task",
                FakeClient({"judge": ("ok", "JUDGE OUTPUT")}),
                run_dir=None, progress_stream=None,
            )

        self.assertTrue(validated_events, "no Validated event was emitted")
        self.assertEqual(validated_events[0].convergence, "judge")
        self.assertEqual(validated_events[0].synthesizers, 0)


def _chain_config(**overrides):
    """3-lane sequential+collect config for progress-event tests."""
    data = {
        "name": "t",
        "convergence": "collect",
        "topology": "sequential",
        "specialists": [
            {"role": "scout", "provider": "openrouter", "model": "s/m", "toolset": ["web"],
             "focus": "gather"},
            {"role": "analyst", "provider": "xai", "model": "grok", "toolset": ["web"],
             "focus": "audit"},
            {"role": "writer", "provider": "openrouter", "model": "w/m", "toolset": [],
             "focus": "write"},
        ],
    }
    data.update(overrides)
    cfg = FleetConfig.from_dict(data)
    resolve(cfg, "/unused")
    return cfg


class TestSequentialProgressEvents(unittest.TestCase):
    """LaneStarted and LaneLaunched(queued=True/False) emission for sequential vs parallel runs."""

    # ------------------------------------------------------------------
    # LaneStarted is a valid ProgressEvent member with the role field
    # ------------------------------------------------------------------

    def test_lane_started_is_in_progress_event_union(self):
        """LaneStarted must be a member of the ProgressEvent union."""
        import typing
        union_args = typing.get_args(ProgressEvent)
        self.assertIn(LaneStarted, union_args)

    def test_lane_started_has_role_field(self):
        """LaneStarted(role=...) constructs correctly and role is readable."""
        ev = LaneStarted(role="scout")
        self.assertEqual(ev.role, "scout")

    # ------------------------------------------------------------------
    # LaneLaunched.queued defaults to False (parallel backward-compat)
    # ------------------------------------------------------------------

    def test_lane_launched_queued_defaults_false(self):
        """LaneLaunched.queued defaults to False — parallel runs are additive; unaffected."""
        ev = LaneLaunched(roles=["web", "social"])
        self.assertFalse(ev.queued)

    def test_lane_launched_queued_true_can_be_constructed(self):
        """LaneLaunched(queued=True) is valid — used by _run_chain roster announcement."""
        ev = LaneLaunched(roles=["scout", "analyst"], queued=True)
        self.assertTrue(ev.queued)

    # ------------------------------------------------------------------
    # Sequential run emits LaneLaunched(queued=True) as the first event
    # ------------------------------------------------------------------

    def test_sequential_emits_queued_lane_launched(self):
        """A sequential run emits LaneLaunched(queued=True) as its first (roster) event."""
        events = []
        run_fleet(_chain_config(), "task", FakeClient(), progress=lambda e: events.append(e))

        launched = [e for e in events if isinstance(e, LaneLaunched)]
        self.assertEqual(len(launched), 1)
        self.assertTrue(launched[0].queued)
        self.assertEqual(launched[0].roles, ["scout", "analyst", "writer"])

    # ------------------------------------------------------------------
    # Sequential run emits LaneStarted per RUNNING lane, none for skipped
    # ------------------------------------------------------------------

    def test_sequential_emits_lane_started_per_running_lane(self):
        """All 3 lanes run → 3 LaneStarted events in config order."""
        events = []
        run_fleet(_chain_config(), "task", FakeClient(), progress=lambda e: events.append(e))

        started = [e for e in events if isinstance(e, LaneStarted)]
        self.assertEqual(len(started), 3)
        self.assertEqual([e.role for e in started], ["scout", "analyst", "writer"])

    def test_sequential_mid_break_lane_started_only_for_running_lanes(self):
        """Analyst fails → writer skipped → 2 LaneStarted (scout, analyst); 3 LaneDone."""
        events = []
        run_fleet(
            _chain_config(), "task",
            FakeClient({"analyst": ("fail", "error")}),
            progress=lambda e: events.append(e),
        )

        started = [e for e in events if isinstance(e, LaneStarted)]
        done = [e for e in events if isinstance(e, LaneDone)]

        # Only 2 LaneStarted: scout (ran) and analyst (ran and failed); writer was skipped.
        self.assertEqual(len(started), 2)
        started_roles = [e.role for e in started]
        self.assertIn("scout", started_roles)
        self.assertIn("analyst", started_roles)
        self.assertNotIn("writer", started_roles)

        # All 3 LaneDone (skipped lane still emits LaneDone so tally reconciles).
        self.assertEqual(len(done), 3)
        done_roles = [e.result.role for e in done]
        self.assertIn("writer", done_roles)

    def test_sequential_first_lane_fail_no_lane_started_for_downstream(self):
        """Scout fails → analyst and writer both skipped → only 1 LaneStarted (scout)."""
        events = []
        run_fleet(
            _chain_config(), "task",
            FakeClient({"scout": ("fail", "boom")}),
            progress=lambda e: events.append(e),
        )

        started = [e for e in events if isinstance(e, LaneStarted)]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].role, "scout")

        done = [e for e in events if isinstance(e, LaneDone)]
        self.assertEqual(len(done), 3)  # all 3 LaneDone (scout failed, analyst+writer skipped)

    # ------------------------------------------------------------------
    # Parallel run: no LaneStarted, LaneLaunched(queued=False)
    # ------------------------------------------------------------------

    def test_parallel_emits_no_lane_started(self):
        """A parallel run emits no LaneStarted events — that event is chain-only."""
        events = []
        run_fleet(
            _config(), "task",
            FakeClient({"synthesizer": ("ok", "S")}),
            progress=lambda e: events.append(e),
        )

        started = [e for e in events if isinstance(e, LaneStarted)]
        self.assertEqual(started, [])

    def test_parallel_lane_launched_queued_is_false(self):
        """A parallel run still emits LaneLaunched(queued=False) — backward-compatible."""
        events = []
        run_fleet(
            _config(), "task",
            FakeClient({"synthesizer": ("ok", "S")}),
            progress=lambda e: events.append(e),
        )

        launched = [e for e in events if isinstance(e, LaneLaunched)]
        self.assertEqual(len(launched), 1)
        self.assertFalse(launched[0].queued)


if __name__ == "__main__":
    unittest.main()
