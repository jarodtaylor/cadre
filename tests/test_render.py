"""Direct unit tests for fleet_engine/render.py render_result.

Constructs FleetResult / AgentResult objects directly (no model calls, no CLI
layer) to cover the three degraded shapes added in U5 and the happy path.
"""

import unittest

from fleet_engine.engine import FleetResult
from fleet_engine.model_client import AgentResult
from fleet_engine.render import render_result


# ---------------------------------------------------------------------------
# Fixtures (make_lane / make_result) — mirrors the repo's make_data pattern
# ---------------------------------------------------------------------------


def make_lane(
    role="web",
    provider="openrouter",
    model="web/model",
    ok=True,
    text=None,
    error=None,
    timed_out=False,
    elapsed_s=1.0,
    toolset=None,
):
    """Return an AgentResult with sensible defaults; caller overrides what it cares about."""
    return AgentResult(
        role=role,
        provider=provider,
        model=model,
        ok=ok,
        text=text if ok else None,
        error=None if ok else (error or "some error"),
        timed_out=timed_out,
        elapsed_s=elapsed_s,
        toolset=toolset if toolset is not None else [],
    )


def make_result(
    fleet="test-fleet",
    task="test task",
    specialists=None,
    synthesis=None,
    synth_ok=None,
    notes=None,
    ok=False,
):
    """Return a FleetResult; caller provides the specialists list and result state."""
    return FleetResult(
        fleet=fleet,
        task=task,
        specialists=specialists or [],
        synthesis=synthesis,
        synth_ok=synth_ok,
        notes=notes or [],
        ok=ok,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath(unittest.TestCase):
    """Normal synthesized result — all specialists ok, synthesis produced."""

    def setUp(self):
        lanes = [
            make_lane(role="web", text="web findings"),
            make_lane(role="social", provider="xai", model="grok", text="social findings"),
        ]
        self.result = make_result(
            specialists=lanes,
            synthesis="THE FINAL SYNTHESIS",
            synth_ok=True,
            ok=True,
        )
        self.rendered = render_result(self.result)

    def test_synthesis_text_present(self):
        self.assertIn("THE FINAL SYNTHESIS", self.rendered)

    def test_synthesized_result_header(self):
        self.assertIn("synthesized result", self.rendered)

    def test_all_lanes_tagged_ok(self):
        self.assertIn("[ok  ] web", self.rendered)
        self.assertIn("[ok  ] social", self.rendered)

    def test_no_fail_or_timeout_tags(self):
        self.assertNotIn("[FAIL]", self.rendered)
        self.assertNotIn("[TIMEOUT]", self.rendered)

    def test_no_all_failed_line(self):
        # The all-failed prominent line must NOT appear when synthesis succeeded.
        self.assertNotIn("synthesis was not attempted", self.rendered)


# ---------------------------------------------------------------------------
# Synthesizer-failed shape
# ---------------------------------------------------------------------------


class TestSynthesizerFailed(unittest.TestCase):
    """synth_ok=False, specialists succeeded, synthesis=None.

    The surviving specialist outputs must be labeled and visible; the header
    must flag no synthesis.
    """

    def setUp(self):
        lanes = [
            make_lane(role="web", text="web output here"),
            make_lane(role="analysis", text="analysis output here"),
        ]
        self.result = make_result(
            specialists=lanes,
            synthesis=None,
            synth_ok=False,
            notes=["synthesizer failed: rate limited"],
            ok=False,
        )
        self.rendered = render_result(self.result)

    def test_header_flags_no_synthesis(self):
        self.assertIn("partial result (no synthesis)", self.rendered)

    def test_surviving_lane_text_visible(self):
        self.assertIn("web output here", self.rendered)
        self.assertIn("analysis output here", self.rendered)

    def test_labeled_sections_present(self):
        self.assertIn("--- web", self.rendered)
        self.assertIn("--- analysis", self.rendered)

    def test_no_all_failed_prominent_line(self):
        # synth_ok is False (not None), so the all-failed line must NOT appear.
        self.assertNotIn("synthesis was not attempted", self.rendered)

    def test_ok_provenance_tags(self):
        self.assertIn("[ok  ] web", self.rendered)
        self.assertIn("[ok  ] analysis", self.rendered)


# ---------------------------------------------------------------------------
# All-specialists-failed shape
# ---------------------------------------------------------------------------


class TestAllSpecialistsFailed(unittest.TestCase):
    """synth_ok=None, all lanes ok=False, synthesis=None.

    The prominent all-failed line must appear with correct counts; no
    fabricated synthesis text.
    """

    def setUp(self):
        lanes = [
            make_lane(role="web", ok=False, error="connection refused"),
            make_lane(role="social", provider="xai", model="grok", ok=False, error="auth error"),
            make_lane(role="analysis", ok=False, error="quota exceeded"),
        ]
        self.result = make_result(
            specialists=lanes,
            synthesis=None,
            synth_ok=None,
            notes=["specialist 'web' failed: connection refused",
                   "specialist 'social' failed: auth error",
                   "specialist 'analysis' failed: quota exceeded",
                   "all specialists failed — no synthesis"],
            ok=False,
        )
        self.rendered = render_result(self.result)

    def test_prominent_all_failed_line_exact_wording(self):
        # Must match capture.py _synthesis_md exactly.
        self.assertIn(
            "No synthesis — 3 of 3 specialists failed; synthesis was not attempted.",
            self.rendered,
        )

    def test_prominent_line_correct_counts(self):
        # Explicitly verify the N-of-N values interpolated correctly.
        self.assertIn("3 of 3", self.rendered)

    def test_header_flags_no_synthesis(self):
        self.assertIn("partial result (no synthesis)", self.rendered)

    def test_no_synthesis_text_fabricated(self):
        # None of the error strings should be mistaken for synthesis body text.
        for phantom in ("THE FINAL", "synthesized", "SYNTH"):
            self.assertNotIn(phantom, self.rendered)

    def test_all_lanes_tagged_fail(self):
        self.assertIn("[FAIL] web", self.rendered)
        self.assertIn("[FAIL] social", self.rendered)
        self.assertIn("[FAIL] analysis", self.rendered)

    def test_provenance_carries_errors(self):
        self.assertIn("connection refused", self.rendered)
        self.assertIn("auth error", self.rendered)
        self.assertIn("quota exceeded", self.rendered)


class TestAllSpecialistsFailedPartialCounts(unittest.TestCase):
    """Variant: 2 failed (not 3) — verifies count interpolation, not just 3-of-3."""

    def setUp(self):
        lanes = [
            make_lane(role="web", ok=False, error="err1"),
            make_lane(role="social", provider="xai", model="grok", ok=False, error="err2"),
        ]
        self.result = make_result(
            specialists=lanes,
            synthesis=None,
            synth_ok=None,
            ok=False,
        )
        self.rendered = render_result(self.result)

    def test_counts_correct(self):
        self.assertIn("2 of 2 specialists failed", self.rendered)


# ---------------------------------------------------------------------------
# Timed-out lane shape
# ---------------------------------------------------------------------------


class TestTimedOutLane(unittest.TestCase):
    """A lane with timed_out=True, ok=False renders [TIMEOUT], NOT [FAIL].

    A co-existing non-timeout failure must still render [FAIL] — both tags
    coexist correctly in the same result.
    """

    def setUp(self):
        lanes = [
            make_lane(
                role="social",
                provider="xai",
                model="grok",
                ok=False,
                timed_out=True,
                error="timed out after 600s",
            ),
            make_lane(
                role="web",
                ok=False,
                timed_out=False,
                error="connection refused",
            ),
            make_lane(role="analysis", text="analysis output"),
        ]
        # Two specialists failed (one timeout, one typed failure); one succeeded.
        # Synthesizer also failed so synth_ok=False so we can test tags alongside content.
        self.result = make_result(
            specialists=lanes,
            synthesis=None,
            synth_ok=False,
            notes=["synthesizer failed: rate limited"],
            ok=False,
        )
        self.rendered = render_result(self.result)

    def test_timeout_lane_tagged_timeout(self):
        self.assertIn("[TIMEOUT] social", self.rendered)

    def test_timeout_lane_not_tagged_fail(self):
        # The timed-out social lane must NOT appear as [FAIL] social.
        self.assertNotIn("[FAIL] social", self.rendered)

    def test_non_timeout_failure_still_tagged_fail(self):
        self.assertIn("[FAIL] web", self.rendered)

    def test_both_tags_coexist(self):
        self.assertIn("[TIMEOUT]", self.rendered)
        self.assertIn("[FAIL]", self.rendered)

    def test_success_lane_still_ok(self):
        self.assertIn("[ok  ] analysis", self.rendered)

    def test_timeout_carries_error_suffix(self):
        self.assertIn("timed out after 600s", self.rendered)

    def test_fail_carries_error_suffix(self):
        self.assertIn("connection refused", self.rendered)


class TestTimedOutLaneAllFailed(unittest.TestCase):
    """All specialists timed out — synth_ok=None; both TIMEOUT tags and all-failed line appear."""

    def setUp(self):
        lanes = [
            make_lane(role="web", ok=False, timed_out=True, error="timed out after 600s"),
            make_lane(role="social", provider="xai", model="grok",
                      ok=False, timed_out=True, error="timed out after 600s"),
        ]
        self.result = make_result(
            specialists=lanes,
            synthesis=None,
            synth_ok=None,
            ok=False,
        )
        self.rendered = render_result(self.result)

    def test_all_failed_prominent_line_present(self):
        self.assertIn(
            "No synthesis — 2 of 2 specialists failed; synthesis was not attempted.",
            self.rendered,
        )

    def test_timeout_tags_present(self):
        self.assertIn("[TIMEOUT] web", self.rendered)
        self.assertIn("[TIMEOUT] social", self.rendered)

    def test_no_fail_tags_when_all_timed_out(self):
        self.assertNotIn("[FAIL]", self.rendered)


if __name__ == "__main__":
    unittest.main()
