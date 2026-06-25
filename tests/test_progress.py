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
    LaneDone,
    LaneLaunched,
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


if __name__ == "__main__":
    unittest.main()
