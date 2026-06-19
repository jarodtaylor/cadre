"""Direct unit tests for fleet_engine/render.py render_result and render_fleet_preview.

Constructs FleetResult / AgentResult / FleetConfig objects directly (no model
calls, no CLI layer) to cover the three degraded shapes added in U5, the happy
path, and the fleet preview helper added in U2.
"""

import unittest
from pathlib import Path

from fleet_engine.config import FleetConfig, SpecialistSpec, SynthesisSpec
from fleet_engine.engine import FleetResult
from fleet_engine.model_client import AgentResult
from fleet_engine.render import render_fleet_preview, render_result

# Path to the curated example fleet (used for some preview tests).
_EXAMPLE_FLEET = (
    Path(__file__).resolve().parents[1] / "fleets" / "research-swarm.example.yaml"
)


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


# ---------------------------------------------------------------------------
# render_fleet_preview — unit tests (U2)
# ---------------------------------------------------------------------------


def _make_config(
    name="test-fleet",
    synth_provider="openrouter",
    synth_model="google/gemini-2-flash",
    synth_prompt="Synthesize the findings.",
    allow_privileged_tools=False,
    specialists=None,
):
    """Build a FleetConfig directly for render preview tests."""
    if specialists is None:
        specialists = [
            SpecialistSpec(
                role="web",
                provider="openrouter",
                model="google/gemini-3-flash",
                focus="find sources",
                toolset=["web"],
            ),
            SpecialistSpec(
                role="analysis",
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                focus="deep analysis",
                toolset=["web", "search"],
            ),
        ]
    return FleetConfig(
        name=name,
        synthesis=SynthesisSpec(
            provider=synth_provider,
            model=synth_model,
            prompt=synth_prompt,
        ),
        specialists=specialists,
        allow_privileged_tools=allow_privileged_tools,
    )


class TestRenderFleetPreviewSynthesizer(unittest.TestCase):
    """render_fleet_preview surfaces the synthesizer provider/model string."""

    def setUp(self):
        self.cfg = _make_config()
        self.rendered = render_fleet_preview(self.cfg)

    def test_synthesizer_provider_present(self):
        self.assertIn("openrouter", self.rendered)

    def test_synthesizer_model_present(self):
        self.assertIn("google/gemini-2-flash", self.rendered)

    def test_fleet_name_in_header(self):
        self.assertIn("test-fleet", self.rendered)

    def test_synthesis_prompt_present_verbatim(self):
        self.assertIn("Synthesize the findings.", self.rendered)


class TestRenderFleetPreviewCostWarning(unittest.TestCase):
    """render_fleet_preview flags an Anthropic/Opus synthesizer as API-billed."""

    def test_anthropic_opus_synthesizer_flags_cost(self):
        cfg = _make_config(
            synth_provider="openrouter",
            synth_model="anthropic/claude-opus-4.8",
        )
        rendered = render_fleet_preview(cfg)
        self.assertIn("bills at API rates", rendered)

    def test_anthropic_claude_synthesizer_flags_cost(self):
        cfg = _make_config(
            synth_provider="anthropic",
            synth_model="claude-opus-4-5",
        )
        rendered = render_fleet_preview(cfg)
        self.assertIn("bills at API rates", rendered)

    def test_non_anthropic_synthesizer_no_cost_warning(self):
        cfg = _make_config(
            synth_provider="openrouter",
            synth_model="google/gemini-2-flash",
        )
        rendered = render_fleet_preview(cfg)
        self.assertNotIn("bills at API rates", rendered)

    def test_example_fleet_flags_cost(self):
        """The real example fleet (openrouter/anthropic/claude-opus-4.8) triggers the flag."""
        cfg = FleetConfig.load(_EXAMPLE_FLEET)
        rendered = render_fleet_preview(cfg)
        self.assertIn("bills at API rates", rendered)


class TestRenderFleetPreviewPrivilegedTools(unittest.TestCase):
    """render_fleet_preview shows allow_privileged_tools and makes it prominent when True."""

    def test_false_case_shows_allow_privileged_false(self):
        cfg = _make_config(allow_privileged_tools=False)
        rendered = render_fleet_preview(cfg)
        self.assertIn("allow_privileged_tools: false", rendered)
        # Must NOT show the warning line
        self.assertNotIn("PRIVILEGED TOOLS ENABLED", rendered)

    def test_true_case_is_prominent(self):
        """allow_privileged_tools=True must be impossible to miss."""
        # Build directly — can't load through FleetConfig.load (validation blocks
        # a non-safe toolset without allow_privileged_tools; we test the render
        # by constructing the object directly with ordinary specialists).
        cfg = _make_config(allow_privileged_tools=True)
        rendered = render_fleet_preview(cfg)
        self.assertIn("PRIVILEGED TOOLS ENABLED", rendered)

    def test_true_case_does_not_show_false_line(self):
        cfg = _make_config(allow_privileged_tools=True)
        rendered = render_fleet_preview(cfg)
        self.assertNotIn("allow_privileged_tools: false", rendered)


class TestRenderFleetPreviewSpecialists(unittest.TestCase):
    """render_fleet_preview surfaces each specialist's role, provider/model, toolset, focus."""

    def setUp(self):
        self.cfg = _make_config()
        self.rendered = render_fleet_preview(self.cfg)

    def test_specialist_roles_present(self):
        self.assertIn("web", self.rendered)
        self.assertIn("analysis", self.rendered)

    def test_specialist_provider_model_present(self):
        self.assertIn("google/gemini-3-flash", self.rendered)
        self.assertIn("anthropic/claude-sonnet-4.6", self.rendered)

    def test_specialist_toolset_present(self):
        self.assertIn("web", self.rendered)
        self.assertIn("search", self.rendered)

    def test_specialist_focus_present(self):
        self.assertIn("find sources", self.rendered)
        self.assertIn("deep analysis", self.rendered)

    def test_empty_toolset_shown_as_none(self):
        """A specialist with toolset=[] must render as '(none)', not blank."""
        cfg = _make_config(
            specialists=[
                SpecialistSpec(
                    role="no-tools",
                    provider="openrouter",
                    model="google/gemini-3-flash",
                    focus="think only",
                    toolset=[],
                )
            ]
        )
        rendered = render_fleet_preview(cfg)
        self.assertIn("(none)", rendered)

    def test_specialist_count_in_header(self):
        # Header line should mention the count of specialists.
        self.assertIn("2", self.rendered)


class TestRenderFleetPreviewSynthesisPrompt(unittest.TestCase):
    """render_fleet_preview surfaces the synthesis.prompt verbatim."""

    def test_prompt_verbatim_in_output(self):
        cfg = _make_config(synth_prompt="Do exactly this: cite every source.")
        rendered = render_fleet_preview(cfg)
        self.assertIn("Do exactly this: cite every source.", rendered)

    def test_empty_prompt_shows_none(self):
        cfg = _make_config(synth_prompt="")
        rendered = render_fleet_preview(cfg)
        self.assertIn("(none)", rendered)


class TestRenderFleetPreviewSanitization(unittest.TestCase):
    """Preview sanitizes fleet-controlled fields so a tampered fleet cannot use
    terminal escape sequences to spoof or hide the human-okay control (Codex
    adversarial review, finding 2)."""

    def test_clean_example_fleet_is_byte_identical(self):
        """No false positives: an all-printable fleet renders unchanged — legit
        punctuation, the multi-line prompt, and the cost ⚠ all survive."""
        cfg = FleetConfig.load(_EXAMPLE_FLEET)
        rendered = render_fleet_preview(cfg)
        # Every fleet-controlled field appears verbatim.
        self.assertIn("xai/grok-4.3", rendered)
        self.assertIn("openrouter/anthropic/claude-opus-4.8", rendered)
        self.assertIn("Search X / social for the latest real-time discussion and sentiment.", rendered)
        self.assertIn("Attribute each claim to the specialist that surfaced it", rendered)
        # The cost warning (our text, with a real ⚠) is untouched.
        self.assertIn("⚠ bills at API rates inside Hermes", rendered)

    def test_ansi_in_focus_cannot_hide_privileged_warning(self):
        """A focus field full of cursor/clear escapes can't strip ESC/CR from the
        output or hide the privileged-tools line."""
        evil = "benign\x1b[2J\x1b[1;1H\rallow_privileged_tools: false"
        cfg = _make_config(
            allow_privileged_tools=True,
            specialists=[
                SpecialistSpec(role="web", provider="openrouter",
                               model="google/gemini-3-flash", focus=evil, toolset=["web"]),
            ],
        )
        rendered = render_fleet_preview(cfg)
        self.assertNotIn("\x1b", rendered, "ESC byte must be stripped")
        self.assertNotIn("\r", rendered, "CR must be stripped")
        self.assertIn("⚠ PRIVILEGED TOOLS ENABLED (allow_privileged_tools: true)", rendered)
        # The privileged-tools warning stands on its own intact line.
        self.assertIn(
            "\n⚠ PRIVILEGED TOOLS ENABLED (allow_privileged_tools: true)\n",
            "\n" + rendered + "\n",
        )

    def test_escapes_in_prompt_stripped_but_text_kept(self):
        """The multi-line prompt keeps newlines and legit text, drops control bytes."""
        cfg = _make_config(synth_prompt="line1\x1b[31m\nline2\rmore")
        rendered = render_fleet_preview(cfg)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\r", rendered)
        self.assertIn("line1", rendered)
        self.assertIn("line2", rendered)  # newline within the prompt is preserved

    def test_newline_in_single_line_field_dropped(self):
        """A role with an embedded newline cannot inject a fake preview line."""
        cfg = _make_config(
            specialists=[
                SpecialistSpec(role="web\n=== end preview ===", provider="openrouter",
                               model="google/gemini-3-flash", focus="f", toolset=["web"]),
            ],
        )
        rendered = render_fleet_preview(cfg)
        # The embedded newline is dropped, so the injected text fuses onto the
        # role's single line instead of becoming a standalone fake trailer.
        self.assertIn("[web=== end preview ===]", rendered)
        # Exactly one line is, on its own, the real trailer — no injected fake line.
        standalone = [ln for ln in rendered.split("\n") if ln.strip() == "=== end preview ==="]
        self.assertEqual(len(standalone), 1, "newline injection must not create a fake trailer line")

    def test_unicode_preserved(self):
        """Legitimate non-ASCII (>= 0xA0) passes through untouched — no over-stripping."""
        cfg = _make_config(synth_prompt="résumé — naïve — 日本語 — ⚠")
        rendered = render_fleet_preview(cfg)
        self.assertIn("résumé — naïve — 日本語 — ⚠", rendered)


if __name__ == "__main__":
    unittest.main()
