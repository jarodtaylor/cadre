"""Direct unit tests for cadre/render.py render_result and render_fleet_preview.

Constructs FleetResult / AgentResult / FleetConfig objects directly (no model
calls, no CLI layer) to cover the three degraded shapes added in U5, the happy
path, and the fleet preview helper added in U2.

U2 tests (``TestProgressRenderer*``) cover the breadcrumb renderer and heartbeat
added in this unit: line formats, tally accounting, sanitisation, concurrency
atomicity, no-survivors path, and clean heartbeat lifecycle.
"""

import io
import threading
import time
import unittest
from pathlib import Path

from cadre.config import FleetConfig, JudgeSpec, SpecialistSpec, SynthesisSpec
from cadre.engine import FleetResult, FleetStatus
from tests.test_engine import _derive_status
from cadre.model_client import AgentResult
from cadre.personas import resolve
from cadre.progress import (
    Completion,
    JudgeDone,
    JudgeStarted,
    LaneDone,
    LaneLaunched,
    LaneStarted,
    RoundStarted,
    RunFolder,
    SynthDone,
    SynthStarted,
    Validated,
)
from cadre.render import (
    ProgressRenderer,
    render_composed_task,
    render_file_inputs,
    render_fleet_preview,
    render_result,
)
from cadre.text_safety import BODY_GUTTER

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
    skipped=False,
):
    """Return an AgentResult with sensible defaults; caller overrides what it cares about."""
    return AgentResult(
        role=role,
        provider=provider,
        model=model,
        ok=ok,
        text=text if ok else None,
        # Skipped lanes have no error (they never ran); ok=False lanes get a default.
        error=None if (ok or skipped) else (error or "some error"),
        timed_out=timed_out,
        elapsed_s=elapsed_s,
        toolset=toolset if toolset is not None else [],
        skipped=skipped,
    )


# Per-run judge marker nonce (R5, #5): judge fixtures carry it in their markers,
# and make_result sets result.judge_marker_nonce to it for judge-mode results so
# the parser (which requires the nonce) matches.
_R_NONCE = "rnd7x2ab"


def make_result(
    fleet="test-fleet",
    task="test task",
    specialists=None,
    synthesis=None,
    synth_ok=None,
    notes=None,
    ok=False,
    convergence="synthesize",
    judge=None,
    judge_ok=None,
    judge_marker_nonce=None,
):
    """Return a FleetResult; caller provides the specialists list and result state."""
    # Judge-mode results get the fixture nonce by default so the nonced markers in
    # the judge-text fixtures parse; callers can override explicitly.
    if judge_marker_nonce is None and convergence == "judge":
        judge_marker_nonce = _R_NONCE
    return FleetResult(
        fleet=fleet,
        task=task,
        specialists=specialists or [],
        synthesis=synthesis,
        synth_ok=synth_ok,
        notes=notes or [],
        status=_derive_status(ok, synth_ok, judge_ok, convergence),
        convergence=convergence,
        judge=judge,
        judge_ok=judge_ok,
        judge_marker_nonce=judge_marker_nonce,
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
    cfg = FleetConfig(
        name=name,
        synthesis=SynthesisSpec(
            provider=synth_provider,
            model=synth_model,
            prompt=synth_prompt,
        ),
        specialists=specialists,
        allow_privileged_tools=allow_privileged_tools,
    )
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


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
    """render_fleet_preview flags a premium synthesizer/judge with a provider-aware
    cost note: per-token providers bill at API rates, OAuth-quota providers (copilot,
    xai, ...) consume quota, and unknown providers default to the API-rates warning."""

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

    def test_oauth_quota_provider_shows_quota_not_api_rates(self):
        # copilot/claude-opus is OAuth quota, not per-token API billing (#8): the
        # premium model still warrants a note, but the wording must be accurate.
        cfg = _make_config(synth_provider="copilot", synth_model="claude-opus-4.8")
        rendered = render_fleet_preview(cfg)
        self.assertIn("OAuth quota, not per-token API billing", rendered)
        self.assertNotIn("bills at API rates", rendered)
        self.assertIn("copilot", rendered)

    def test_xai_oauth_variant_classified_as_quota(self):
        cfg = _make_config(synth_provider="xai-oauth", synth_model="claude-opus-4.8")
        rendered = render_fleet_preview(cfg)
        self.assertIn("OAuth quota, not per-token API billing", rendered)
        self.assertNotIn("bills at API rates", rendered)

    def test_unknown_provider_with_premium_model_defaults_to_api_rates(self):
        # False-negative-averse: an unrecognized provider falls through to the
        # API-rates warning rather than being silently treated as quota.
        cfg = _make_config(synth_provider="some-new-vendor", synth_model="claude-opus-4.8")
        rendered = render_fleet_preview(cfg)
        self.assertIn("bills at API rates", rendered)
        self.assertNotIn("OAuth quota", rendered)


class TestRenderJudgePreviewCostWarning(unittest.TestCase):
    """The judge call site is provider-aware too: a per-token judge bills at API
    rates; an OAuth-quota judge (e.g. copilot) shows the honest quota note."""

    def test_judge_per_token_provider_bills_at_api_rates(self):
        cfg = _make_judge_config(judge_provider="openrouter", judge_model="anthropic/claude-opus-4.8")
        rendered = render_fleet_preview(cfg)
        self.assertIn("bills at API rates", rendered)

    def test_judge_oauth_quota_provider_shows_quota(self):
        cfg = _make_judge_config(judge_provider="copilot", judge_model="claude-opus-4.8")
        rendered = render_fleet_preview(cfg)
        self.assertIn("OAuth quota, not per-token API billing", rendered)
        self.assertNotIn("bills at API rates", rendered)


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
        # Header line should mention the formatted specialist count.
        self.assertIn("Specialists (2)", self.rendered)


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
        resolve(cfg, "/unused")  # focus-only fleet: sets effective_instruction = focus, zero I/O
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


# ---------------------------------------------------------------------------
# New tests: persona-lane preview format (U3)
# ---------------------------------------------------------------------------


class TestRenderFleetPreviewPersonaLane(unittest.TestCase):
    """render_fleet_preview renders persona lanes differently from focus lanes.

    Persona lane: two-line block — "persona: <name>" + indented body.
    Focus lane:   single-line "focus: <text>".
    persona name passes through _sanitize like every other fleet-controlled field.
    """

    def setUp(self):
        import tempfile, os, shutil
        from cadre.config import SpecialistSpec
        self._tmp = tempfile.mkdtemp()
        self.pool = os.path.realpath(self._tmp)
        # Write a minimal persona file.
        persona_body = "You are a coherence reviewer.\n\nCheck for internal consistency.\n"
        with open(os.path.join(self.pool, "coherence-reviewer.md"), "w", encoding="utf-8") as f:
            f.write(persona_body)
        self.persona_body = persona_body

        cfg = FleetConfig(
            name="test-persona-fleet",
            synthesis=SynthesisSpec(
                provider="openrouter",
                model="google/gemini-2-flash",
                prompt="Synthesize.",
            ),
            specialists=[
                SpecialistSpec(
                    role="coherence",
                    provider="openrouter",
                    model="anthropic/claude-sonnet-4.6",
                    persona="coherence-reviewer",
                    toolset=[],
                ),
                SpecialistSpec(
                    role="breadth",
                    provider="xai",
                    model="grok-4.3",
                    focus="Scan broadly.",
                    toolset=["web"],
                ),
            ],
        )
        resolve(cfg, self.pool)
        self.cfg = cfg
        self.rendered = render_fleet_preview(cfg)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp)

    def test_persona_name_label_present(self):
        """Persona lane renders 'persona: <name>' as its label."""
        self.assertIn("persona: coherence-reviewer", self.rendered)

    def test_persona_body_present_in_output(self):
        """The resolved persona text appears in the preview output."""
        self.assertIn("You are a coherence reviewer.", self.rendered)
        self.assertIn("Check for internal consistency.", self.rendered)

    def test_focus_lane_still_renders_focus_label(self):
        """Focus lane still renders 'focus: <text>', unchanged."""
        self.assertIn("focus: Scan broadly.", self.rendered)

    def test_persona_lane_no_focus_label(self):
        """Persona lane must NOT render a 'focus:' label for the coherence specialist."""
        # The coherence specialist has no focus, only a persona — no focus label.
        # We check the rendered block for the coherence role specifically.
        lines = self.rendered.split("\n")
        coherence_block_start = next(
            (i for i, ln in enumerate(lines) if "coherence" in ln and "[coherence]" in ln),
            None,
        )
        self.assertIsNotNone(coherence_block_start, "persona lane block must be present")
        if coherence_block_start is not None:
            # Find the next specialist block or end.
            block_lines = []
            for ln in lines[coherence_block_start + 1:]:
                if ln.startswith("  ["):  # next specialist
                    break
                block_lines.append(ln)
            block_text = "\n".join(block_lines)
            self.assertNotIn("focus:", block_text,
                             "persona lane must not render a 'focus:' label")

    def test_persona_name_is_sanitized(self):
        """persona name passes through _sanitize — escapes are stripped before render."""
        # Bypass config validation by constructing a SpecialistSpec directly with
        # a persona name containing a terminal escape, then build a FleetConfig and
        # render the preview. The ESC byte must not appear in the output.
        evil_spec = SpecialistSpec(
            role="evil-role",
            provider="openrouter",
            model="google/gemini-3-flash",
            persona="evil\x1b[2Kname",
            effective_instruction="some review instruction body",
            toolset=[],
        )
        cfg_evil = FleetConfig(
            name="evil-fleet",
            synthesis=SynthesisSpec(
                provider="openrouter",
                model="google/gemini-2-flash",
                prompt="Synthesize.",
            ),
            specialists=[evil_spec],
        )
        rendered_evil = render_fleet_preview(cfg_evil)
        self.assertNotIn("\x1b", rendered_evil, "ESC byte must be stripped from persona name in preview")

    def test_esc_in_persona_body_stripped(self):
        """Terminal ESC bytes in persona file body are stripped in preview output.

        The resolver stores raw bytes; the render layer sanitizes (KTD6).
        """
        import os, tempfile, shutil
        from cadre.config import SpecialistSpec
        tmp = tempfile.mkdtemp()
        pool = os.path.realpath(tmp)
        try:
            evil_body = "Safe text\x1b[2Jevil override\n"
            with open(os.path.join(pool, "evil-persona.md"), "w", encoding="utf-8") as f:
                f.write(evil_body)
            cfg2 = FleetConfig(
                name="evil-fleet",
                synthesis=SynthesisSpec(
                    provider="openrouter",
                    model="google/gemini-2-flash",
                    prompt="S",
                ),
                specialists=[
                    SpecialistSpec(
                        role="r",
                        provider="openrouter",
                        model="google/gemini-2-flash",
                        persona="evil-persona",
                        toolset=[],
                    ),
                ],
            )
            resolve(cfg2, pool)
            rendered2 = render_fleet_preview(cfg2)
            self.assertNotIn("\x1b", rendered2, "ESC byte must be stripped from persona body in preview")
            self.assertIn("Safe text", rendered2)
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# New tests: collect-convergence in render_fleet_preview (U3)
# ---------------------------------------------------------------------------


def _make_collect_config(allow_privileged_tools=False):
    """Build a collect FleetConfig (synthesis=None) for preview tests."""
    cfg = FleetConfig(
        name="test-collect-fleet",
        synthesis=None,
        convergence="collect",
        specialists=[
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
        ],
        allow_privileged_tools=allow_privileged_tools,
    )
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


class TestRenderFleetPreviewCollect(unittest.TestCase):
    """render_fleet_preview handles collect fleets — no synthesizer, no prompt block."""

    def setUp(self):
        self.cfg = _make_collect_config()
        self.rendered = render_fleet_preview(self.cfg)

    def test_collect_convergence_line_present(self):
        self.assertIn("collect (no synthesizer)", self.rendered)

    def test_no_synthesizer_line(self):
        self.assertNotIn("Synthesizer:", self.rendered)

    def test_no_synthesis_prompt_block(self):
        self.assertNotIn("Synthesis prompt:", self.rendered)

    def test_specialists_still_listed(self):
        self.assertIn("web", self.rendered)
        self.assertIn("analysis", self.rendered)
        self.assertIn("Specialists (2)", self.rendered)

    def test_no_raises(self):
        """render_fleet_preview must not AttributeError on synthesis=None."""
        # setUp already called it — if it raised, setUp would have failed.
        self.assertIsInstance(self.rendered, str)

    def test_privileged_tools_still_renders_collect_true(self):
        """A collect fleet with allow_privileged_tools=True must still show the warning."""
        cfg = _make_collect_config(allow_privileged_tools=True)
        rendered = render_fleet_preview(cfg)
        self.assertIn("PRIVILEGED TOOLS ENABLED", rendered)
        self.assertIn("collect (no synthesizer)", rendered)

    def test_privileged_tools_false_renders_collect(self):
        """A collect fleet with allow_privileged_tools=False shows the false line."""
        self.assertIn("allow_privileged_tools: false", self.rendered)
        self.assertIn("collect (no synthesizer)", self.rendered)


class TestRenderFleetPreviewCollectKeyedOnConvergence(unittest.TestCase):
    """render_fleet_preview keys on config.convergence, not config.synthesis being None.

    A collect fleet that carries a stray synthesis: block must still preview
    as collect — the explicit convergence field is the source of truth (KTD1).
    """

    def test_stray_synthesis_block_still_previews_as_collect(self):
        """convergence="collect" wins even when synthesis is non-None."""
        cfg = FleetConfig(
            name="stray-synth-fleet",
            synthesis=SynthesisSpec(
                provider="openrouter",
                model="google/gemini-2-flash",
                prompt="Should not appear.",
            ),
            convergence="collect",
            specialists=[
                SpecialistSpec(
                    role="web",
                    provider="openrouter",
                    model="google/gemini-3-flash",
                    toolset=["web"],
                ),
            ],
        )
        rendered = render_fleet_preview(cfg)
        self.assertIn("collect (no synthesizer)", rendered)
        self.assertNotIn("Synthesizer:", rendered)
        self.assertNotIn("Synthesis prompt:", rendered)
        self.assertNotIn("Should not appear.", rendered)


# ---------------------------------------------------------------------------
# New tests: collect-convergence in render_result (U3)
# ---------------------------------------------------------------------------


class TestRenderResultCollectSuccess(unittest.TestCase):
    """render_result on a successful collect run — the critical KTD2 negative test.

    ok=True, synthesis=None, synth_ok=None, convergence="collect".
    The 'synthesis was not attempted' line must NEVER appear; the header must
    be 'collect result', not 'synthesized result' or 'partial result'.
    """

    def setUp(self):
        lanes = [
            make_lane(role="web", text="web findings here"),
            make_lane(role="analysis", text="analysis findings here"),
        ]
        self.result = make_result(
            specialists=lanes,
            synthesis=None,
            synth_ok=None,
            ok=True,
            convergence="collect",
        )
        self.rendered = render_result(self.result)

    def test_collect_result_header_present(self):
        self.assertIn("collect result", self.rendered)

    def test_no_synthesis_was_not_attempted_line(self):
        """The load-bearing negative assertion: the false-failure line must not fire."""
        self.assertNotIn("synthesis was not attempted", self.rendered)

    def test_no_partial_result_header(self):
        """'partial result' is a synthesize-mode label; must not appear on collect."""
        self.assertNotIn("partial result", self.rendered)

    def test_no_synthesized_result_header(self):
        """'synthesized result' is a synthesize-mode label; must not appear on collect."""
        self.assertNotIn("synthesized result", self.rendered)

    def test_attributed_specialist_blocks_present(self):
        """Specialist text is surfaced in labeled sections (the elif result.successes branch)."""
        self.assertIn("--- web", self.rendered)
        self.assertIn("web findings here", self.rendered)
        self.assertIn("--- analysis", self.rendered)
        self.assertIn("analysis findings here", self.rendered)

    def test_provenance_rows_present(self):
        self.assertIn("[ok  ] web", self.rendered)
        self.assertIn("[ok  ] analysis", self.rendered)


class TestRenderResultCollectAllFailed(unittest.TestCase):
    """render_result on a collect run where all specialists failed.

    ok=False, synthesis=None, synth_ok=None, convergence="collect".
    This state is closest to the original bug trigger (synth_ok is None AND ok is
    False) — the guard must still suppress 'synthesis was not attempted'.
    """

    def setUp(self):
        lanes = [
            make_lane(role="web", ok=False, error="connection refused"),
            make_lane(role="analysis", ok=False, error="quota exceeded"),
        ]
        self.result = make_result(
            specialists=lanes,
            synthesis=None,
            synth_ok=None,
            ok=False,
            convergence="collect",
        )
        self.rendered = render_result(self.result)

    def test_collect_failed_header_present(self):
        self.assertIn("collect result — all specialists failed", self.rendered)

    def test_no_synthesis_was_not_attempted_line(self):
        """Guard must suppress this line for collect even when ok=False."""
        self.assertNotIn("synthesis was not attempted", self.rendered)

    def test_no_partial_result_header(self):
        self.assertNotIn("partial result", self.rendered)

    def test_no_synthesized_result_header(self):
        self.assertNotIn("synthesized result", self.rendered)

    def test_provenance_rows_tagged_fail(self):
        self.assertIn("[FAIL] web", self.rendered)
        self.assertIn("[FAIL] analysis", self.rendered)


# ---------------------------------------------------------------------------
# New tests: render_result sanitizes config-derived fields (FIX 2)
# ---------------------------------------------------------------------------


class TestRenderResultSanitizesConfigFields(unittest.TestCase):
    """render_result sanitizes fleet/role/provider/model (config-derived), not model output."""

    def test_newline_in_role_cannot_forge_provenance_row(self):
        """A role containing a newline cannot inject a fake provenance row."""
        forged_role = "web\n[ok  ] ghost (x/y)"
        lane = make_lane(role=forged_role, provider="openrouter", model="web/model", ok=True, text="output")
        result = make_result(specialists=[lane], synthesis="SYNTH", synth_ok=True, ok=True)
        rendered = render_result(result)
        # The injected newline is dropped, so there must be no standalone line that
        # exactly matches the forged provenance text.
        standalone = [ln for ln in rendered.split("\n") if ln.strip() == "[ok  ] ghost (x/y)"]
        self.assertEqual(len(standalone), 0, "forged provenance row must not appear as a standalone line")

    def test_esc_in_provider_is_stripped(self):
        """An ESC byte in provider cannot sneak through to the terminal."""
        lane = make_lane(role="web", provider="open\x1b[31mrouter", model="bad/model", ok=True, text="out")
        result = make_result(specialists=[lane], synthesis="SYNTH", synth_ok=True, ok=True)
        rendered = render_result(result)
        self.assertNotIn("\x1b", rendered)

    def test_model_output_is_sanitized(self):
        """r.text and result.synthesis ARE stripped (#5 U2 — model output can spoof this surface)."""
        lane = make_lane(role="web", ok=True, text="output with \x1b[31m escape")
        result = make_result(specialists=[lane], synthesis="synth with \x1b escape", synth_ok=True, ok=True)
        rendered = render_result(result)
        # Synthesize mode renders the synthesis body (not per-lane text); its ESC is
        # stripped while the visible words survive.
        self.assertNotIn("\x1b", rendered)
        self.assertIn("synth with", rendered)

    def test_notes_section_is_sanitized(self):
        """result.notes embed role + adapter error text; the notes block on the same
        surface must be sanitized too (#5 U2), not just the provenance rows."""
        lane = make_lane(role="web", ok=True, text="ok")
        result = make_result(
            specialists=[lane], synthesis="S", synth_ok=True, ok=True,
            notes=["specialist 'web' failed: boom \x1b[2K forged"],
        )
        rendered = render_result(result)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("boom", rendered)

    def test_specialist_text_is_sanitized_in_collect_mode(self):
        """In collect mode the per-lane r.text renders — and is sanitized (#5 U2)."""
        lane = make_lane(role="web", ok=True, text="finding with \x1b[31m escape")
        result = make_result(
            specialists=[lane], convergence="collect", synthesis=None, ok=True,
        )
        rendered = render_result(result)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("finding with", rendered)


# ---------------------------------------------------------------------------
# New tests: _sanitize strips Unicode line separators and bidi controls (FIX 1)
# ---------------------------------------------------------------------------


class TestSanitizeUnicodeExclusions(unittest.TestCase):
    """_sanitize drops Unicode line/paragraph separators and bidi format controls."""

    def setUp(self):
        from cadre.render import _sanitize
        self._sanitize = _sanitize

    def test_line_separator_stripped_single_line(self):
        """U+2028 (LINE SEPARATOR) is removed from single-line field."""
        text = "before after"
        result = self._sanitize(text, multiline=False)
        self.assertNotIn(" ", result)
        self.assertIn("before", result)
        self.assertIn("after", result)

    def test_paragraph_separator_stripped_single_line(self):
        """U+2029 (PARAGRAPH SEPARATOR) is removed from single-line field."""
        text = "before after"
        result = self._sanitize(text, multiline=False)
        self.assertNotIn(" ", result)

    def test_bidi_override_stripped(self):
        """U+202E (RIGHT-TO-LEFT OVERRIDE) is removed."""
        text = "safe‮evil"
        result = self._sanitize(text)
        self.assertNotIn("‮", result)

    def test_legit_latin_extended_survives(self):
        """é (U+00E9) is legitimate and must pass through untouched."""
        result = self._sanitize("résumé")
        self.assertEqual(result, "résumé")

    def test_em_dash_survives(self):
        """— (U+2014, em-dash) is legitimate and must survive."""
        result = self._sanitize("word — word")
        self.assertIn("—", result)

    def test_tab_preserved_in_multiline_only(self):
        """\\t passes through in multiline mode but is stripped in single-line mode."""
        text = "col1\tcol2"
        self.assertIn("\t", self._sanitize(text, multiline=True))
        self.assertNotIn("\t", self._sanitize(text, multiline=False))

    def test_newline_preserved_in_multiline_only(self):
        """\\n passes through in multiline mode but is stripped in single-line mode."""
        text = "line1\nline2"
        self.assertIn("\n", self._sanitize(text, multiline=True))
        self.assertNotIn("\n", self._sanitize(text, multiline=False))


# ---------------------------------------------------------------------------
# New test: render_result multiline synthesis preserves tab (FIX 1 / multiline)
# ---------------------------------------------------------------------------


class TestRenderResultTabInSynthesis(unittest.TestCase):
    """Model output IS sanitized with multiline=True (#5 U2), which preserves tabs
    and newlines while stripping control bytes; this test verifies a multiline field
    with a tab keeps the tab under multiline=True."""

    def setUp(self):
        from cadre.render import _sanitize
        self._sanitize = _sanitize

    def test_tab_in_multiline_synthesis_prompt_preserved(self):
        """A synthesis prompt with \\t renders with the tab preserved (multiline=True)."""
        prompt = "Column A:\tColumn B"
        result = self._sanitize(prompt, multiline=True)
        self.assertIn("\t", result)
        self.assertIn("Column A:", result)
        self.assertIn("Column B", result)


# ---------------------------------------------------------------------------
# ProgressRenderer — U2 tests
# ---------------------------------------------------------------------------

# Tiny heartbeat interval for tests so they run fast.
_HB_INTERVAL = 0.05


def _make_renderer(stream=None, filename_for=None, interval_s=_HB_INTERVAL):
    """Build a ProgressRenderer pointed at an io.StringIO (default) or a given stream."""
    if stream is None:
        stream = io.StringIO()
    return ProgressRenderer(stream=stream, filename_for=filename_for, interval_s=interval_s), stream


def _lines(stream: io.StringIO) -> list[str]:
    """Return non-empty lines written to the StringIO."""
    return [ln for ln in stream.getvalue().split("\n") if ln]


class TestProgressRendererLineFormats(unittest.TestCase):
    """Each event type renders its expected ``[cadre] …`` line (Covers AE1)."""

    def _emit_and_get(self, event, filename_for=None):
        r, stream = _make_renderer(filename_for=filename_for)
        r.emit(event)
        return _lines(stream)

    def test_validated_format(self):
        lines = self._emit_and_get(Validated(fleet="my-fleet", specialists=3, synthesizers=1))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] validated fleet 'my-fleet' — 3 specialists, 1 synthesizer(s)")

    def test_run_folder_format(self):
        lines = self._emit_and_get(RunFolder(path="/tmp/cadre/run-001"))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] run folder: /tmp/cadre/run-001")

    def test_lane_launched_format(self):
        lines = self._emit_and_get(LaneLaunched(roles=["web", "social", "analysis"]))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] launched 3 specialists: web, social, analysis")

    def test_lane_done_with_capture(self):
        """LaneDone with ``filename_for`` produces the ``-> <filename>`` suffix."""
        result = make_lane(role="web", ok=True, elapsed_s=12.3)
        lines = self._emit_and_get(
            LaneDone(result=result),
            filename_for=lambda role: f"{role}.md",
        )
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] lane web ok 12.3s -> web.md")

    def test_lane_done_without_capture(self):
        """LaneDone without ``filename_for`` omits the filename (R13)."""
        result = make_lane(role="web", ok=True, elapsed_s=5.0)
        lines = self._emit_and_get(LaneDone(result=result))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] lane web ok 5.0s")

    def test_lane_done_failed_label(self):
        result = make_lane(role="scan", ok=False, elapsed_s=3.7)
        lines = self._emit_and_get(LaneDone(result=result))
        self.assertEqual(lines[0], "[cadre] lane scan failed 3.7s")

    def test_lane_done_unknown_elapsed_renders_placeholder(self):
        """A lane with elapsed_s=None (defensive guard) renders '?.?s', not a crash."""
        result = make_lane(role="web", ok=True, elapsed_s=None)
        lines = self._emit_and_get(LaneDone(result=result))
        self.assertEqual(lines[0], "[cadre] lane web ok ?.?s")

    def test_lane_done_timed_out_label(self):
        result = make_lane(role="analysis", ok=False, timed_out=True, elapsed_s=600.0)
        lines = self._emit_and_get(LaneDone(result=result))
        self.assertEqual(lines[0], "[cadre] lane analysis timed-out 600.0s")

    def test_synth_started_format(self):
        lines = self._emit_and_get(SynthStarted(survivors=2))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] synthesizing over 2 survivor(s)")

    def test_synth_done_ok_format(self):
        lines = self._emit_and_get(SynthDone(outcome="ok", elapsed_s=8.2))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] synthesis ok 8.2s")

    def test_synth_done_failed_format(self):
        lines = self._emit_and_get(SynthDone(outcome="failed", elapsed_s=1.0))
        self.assertEqual(lines[0], "[cadre] synthesis failed 1.0s")

    def test_completion_with_run_dir(self):
        lines = self._emit_and_get(Completion(elapsed_s=45.6, run_dir="/tmp/cadre/run-001"))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] done in 45.6s — run folder: /tmp/cadre/run-001")

    def test_completion_without_run_dir(self):
        """Completion with run_dir=None omits the run-folder suffix (capture off, R13)."""
        lines = self._emit_and_get(Completion(elapsed_s=30.0, run_dir=None))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] done in 30.0s")


class TestProgressRendererTally(unittest.TestCase):
    """Tally reflects done/failed honestly as LaneDone events arrive (Covers AE2/AE3)."""

    def setUp(self):
        self.r, self.stream = _make_renderer()

    def _tally(self):
        return self.r._total, self.r._done, self.r._failed

    def test_lane_launched_sets_total(self):
        self.r.emit(LaneLaunched(roles=["web", "social", "analysis"]))
        total, done, failed = self._tally()
        self.assertEqual(total, 3)
        self.assertEqual(done, 0)
        self.assertEqual(failed, 0)

    def test_ok_lane_increments_done(self):
        self.r.emit(LaneLaunched(roles=["web"]))
        self.r.emit(LaneDone(result=make_lane(role="web", ok=True, elapsed_s=1.0)))
        total, done, failed = self._tally()
        self.assertEqual(done, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(total - done - failed, 0)  # active=0

    def test_failed_lane_increments_failed(self):
        self.r.emit(LaneLaunched(roles=["web", "scan"]))
        self.r.emit(LaneDone(result=make_lane(role="web", ok=False, elapsed_s=2.0)))
        total, done, failed = self._tally()
        self.assertEqual(done, 0)
        self.assertEqual(failed, 1)
        self.assertEqual(total - done - failed, 1)  # active=1

    def test_timed_out_lane_increments_failed(self):
        """A timed-out lane counts as failed, not done (Covers AE3)."""
        self.r.emit(LaneLaunched(roles=["web"]))
        self.r.emit(LaneDone(result=make_lane(role="web", ok=False, timed_out=True, elapsed_s=600.0)))
        _, done, failed = self._tally()
        self.assertEqual(done, 0)
        self.assertEqual(failed, 1)

    def test_mixed_lanes_tally_correctly(self):
        self.r.emit(LaneLaunched(roles=["web", "social", "scan"]))
        self.r.emit(LaneDone(result=make_lane(role="web", ok=True, elapsed_s=10.0)))
        self.r.emit(LaneDone(result=make_lane(role="social", ok=False, elapsed_s=5.0)))
        self.r.emit(LaneDone(result=make_lane(role="scan", ok=False, timed_out=True, elapsed_s=600.0)))
        total, done, failed = self._tally()
        self.assertEqual(total, 3)
        self.assertEqual(done, 1)
        self.assertEqual(failed, 2)
        self.assertEqual(total - done - failed, 0)  # all accounted for


class TestProgressRendererHeartbeat(unittest.TestCase):
    """The heartbeat fires on cadence and shows the current tally."""

    def test_heartbeat_fires_with_correct_shape(self):
        """A heartbeat line with correct prefix and tally appears after ``interval_s``."""
        r, stream = _make_renderer(interval_s=_HB_INTERVAL)
        r.emit(LaneLaunched(roles=["web", "social"]))
        r.emit(LaneDone(result=make_lane(role="web", ok=True, elapsed_s=1.0)))
        r.start_heartbeat()
        # Sleep long enough for at least one tick.
        time.sleep(_HB_INTERVAL * 3)
        r.stop_heartbeat()

        hb_lines = [ln for ln in _lines(stream) if "[cadre] heartbeat" in ln]
        self.assertGreater(len(hb_lines), 0, "at least one heartbeat line must appear")
        # Shape: [cadre] heartbeat mm:ss active=A/T done=D failed=F skipped=S
        pattern = r"^\[cadre\] heartbeat \d{2}:\d{2} active=\d+/\d+ done=\d+ failed=\d+ skipped=\d+$"
        for ln in hb_lines:
            self.assertRegex(ln, pattern, f"heartbeat line has wrong shape: {ln!r}")

    def test_heartbeat_reflects_live_tally(self):
        """The heartbeat tally values match the emitted lane-done events."""
        r, stream = _make_renderer(interval_s=_HB_INTERVAL)
        r.emit(LaneLaunched(roles=["web", "social", "scan"]))
        r.emit(LaneDone(result=make_lane(role="web", ok=True, elapsed_s=1.0)))
        r.emit(LaneDone(result=make_lane(role="social", ok=False, elapsed_s=2.0)))
        # scan still active
        r.start_heartbeat()
        time.sleep(_HB_INTERVAL * 3)
        r.stop_heartbeat()

        hb_lines = [ln for ln in _lines(stream) if "[cadre] heartbeat" in ln]
        self.assertGreater(len(hb_lines), 0)
        # All heartbeat lines must show: active=1/3 done=1 failed=1
        for ln in hb_lines:
            self.assertIn("active=1/3", ln, f"unexpected active count: {ln!r}")
            self.assertIn("done=1", ln, f"unexpected done count: {ln!r}")
            self.assertIn("failed=1", ln, f"unexpected failed count: {ln!r}")

    def test_no_heartbeat_line_before_start(self):
        """Before ``start_heartbeat`` is called, no heartbeat line appears."""
        r, stream = _make_renderer(interval_s=_HB_INTERVAL)
        r.emit(LaneLaunched(roles=["web"]))
        # Do NOT call start_heartbeat.
        time.sleep(_HB_INTERVAL * 2)
        hb_lines = [ln for ln in _lines(stream) if "[cadre] heartbeat" in ln]
        self.assertEqual(len(hb_lines), 0)


class TestSequentialHeartbeat(unittest.TestCase):
    """On the sequential path (queued launch) the heartbeat splits the tally into
    the truly-running lane (0 or 1) plus a queued= field, instead of the parallel
    active-counts-everyone tally that reads as if the chain ran concurrently (#40).
    Formats ``_heartbeat_line`` directly — deterministic, no timer."""

    def test_sequential_heartbeat_splits_running_and_queued(self):
        r, _ = _make_renderer()
        r.emit(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        r.emit(LaneStarted(role="scout"))
        r.emit(LaneDone(result=make_lane(role="scout", ok=True, elapsed_s=1.0)))
        r.emit(LaneStarted(role="analyst"))   # analyst running; writer still queued
        line = r._heartbeat_line(60)
        self.assertIn("active=1/3", line)     # exactly one lane runs
        self.assertIn("queued=1", line)       # writer not yet started
        self.assertIn("done=1", line)
        self.assertIn("failed=0", line)
        self.assertIn("skipped=0", line)

    def test_sequential_heartbeat_between_stages_shows_zero_running(self):
        r, _ = _make_renderer()
        r.emit(LaneLaunched(roles=["a", "b"], queued=True))
        r.emit(LaneStarted(role="a"))
        r.emit(LaneDone(result=make_lane(role="a", ok=True, elapsed_s=1.0)))
        # 'a' finished, 'b' not started yet — nothing is running.
        line = r._heartbeat_line(30)
        self.assertIn("active=0/2", line)
        self.assertIn("queued=1", line)
        self.assertIn("done=1", line)

    def test_sequential_heartbeat_excludes_skipped_from_queued(self):
        r, _ = _make_renderer()
        r.emit(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        r.emit(LaneStarted(role="scout"))
        r.emit(LaneDone(result=make_lane(role="scout", ok=False, elapsed_s=1.0)))
        # Chain broke: downstream lanes skipped (never started).
        r.emit(LaneDone(result=make_lane(role="analyst", skipped=True, ok=False)))
        r.emit(LaneDone(result=make_lane(role="writer", skipped=True, ok=False)))
        line = r._heartbeat_line(90)
        self.assertIn("active=0/3", line)     # nothing running
        self.assertIn("queued=0", line)       # skipped lanes are NOT queued
        self.assertIn("failed=1", line)
        self.assertIn("skipped=2", line)

    def test_parallel_heartbeat_has_no_queued_field(self):
        """The parallel path is unchanged: active = not-yet-finished, and NO queued= field."""
        r, _ = _make_renderer()
        r.emit(LaneLaunched(roles=["web", "social", "scan"]))   # queued defaults False
        r.emit(LaneDone(result=make_lane(role="web", ok=True, elapsed_s=1.0)))
        line = r._heartbeat_line(15)
        self.assertIn("active=2/3", line)     # two still running (parallel)
        self.assertNotIn("queued=", line)
        self.assertIn("done=1", line)

    def test_no_heartbeat_after_stop(self):
        """After ``stop_heartbeat``, no further heartbeat lines appear."""
        r, stream = _make_renderer(interval_s=_HB_INTERVAL)
        r.start_heartbeat()
        time.sleep(_HB_INTERVAL * 2)
        r.stop_heartbeat()
        count_before = len([ln for ln in _lines(stream) if "[cadre] heartbeat" in ln])
        # Wait two more intervals — no new ticks should fire.
        time.sleep(_HB_INTERVAL * 2)
        count_after = len([ln for ln in _lines(stream) if "[cadre] heartbeat" in ln])
        self.assertEqual(count_before, count_after, "no heartbeat lines after stop_heartbeat")

    def test_stop_before_start_is_idempotent(self):
        """Calling ``stop_heartbeat`` before ``start_heartbeat`` must not raise."""
        r, _ = _make_renderer()
        r.stop_heartbeat()  # must not raise

    def test_double_stop_is_idempotent(self):
        """Calling ``stop_heartbeat`` twice must not raise."""
        r, _ = _make_renderer(interval_s=_HB_INTERVAL)
        r.start_heartbeat()
        r.stop_heartbeat()
        r.stop_heartbeat()  # must not raise

    def test_no_leaked_heartbeat_thread_after_stop(self):
        """After ``stop_heartbeat``, no thread named 'cadre-heartbeat' remains alive."""
        r, _ = _make_renderer(interval_s=_HB_INTERVAL)
        r.start_heartbeat()
        time.sleep(_HB_INTERVAL * 2)
        r.stop_heartbeat()
        # Give the join a brief moment to complete, then check.
        time.sleep(0.02)
        alive_names = [t.name for t in threading.enumerate() if t.is_alive()]
        self.assertNotIn("cadre-heartbeat", alive_names)


class TestProgressRendererSanitization(unittest.TestCase):
    """Config-sourced fields are sanitised; terminal escapes are neutralised (KTD4)."""

    def test_esc_in_fleet_name_stripped_from_validated(self):
        """ESC byte in fleet name must not appear in the Validated breadcrumb."""
        r, stream = _make_renderer()
        r.emit(Validated(fleet="evil\x1b[2Jname", specialists=1, synthesizers=1))
        out = stream.getvalue()
        self.assertNotIn("\x1b", out, "ESC byte must be stripped from fleet name")
        self.assertIn("[cadre] validated fleet", out)

    def test_cr_in_fleet_name_stripped(self):
        """CR byte in fleet name must not appear in the Validated breadcrumb."""
        r, stream = _make_renderer()
        r.emit(Validated(fleet="fleet\rfake", specialists=2, synthesizers=1))
        out = stream.getvalue()
        self.assertNotIn("\r", out, "CR must be stripped from fleet name")

    def test_esc_in_role_stripped_from_lane_done(self):
        """ESC byte in role must not appear in the LaneDone breadcrumb."""
        # Use capture off (filename_for=None) so we don't need a map key lookup.
        result = make_lane(role="web\x1b[31m", ok=True, elapsed_s=1.0)
        r, stream = _make_renderer(filename_for=None)
        r.emit(LaneLaunched(roles=["web\x1b[31m"]))
        r.emit(LaneDone(result=result))
        out = stream.getvalue()
        self.assertNotIn("\x1b", out, "ESC byte must be stripped from role in LaneDone")

    def test_newline_in_role_cannot_forge_breadcrumb_line(self):
        """A newline in a role name cannot inject a fake breadcrumb line."""
        forged = "web\n[cadre] launch fake"
        result = make_lane(role=forged, ok=True, elapsed_s=1.0)
        r, stream = _make_renderer(filename_for=None)
        r.emit(LaneDone(result=result))
        output_lines = _lines(stream)
        # No standalone line that exactly matches the forged injection.
        forged_standalone = [ln for ln in output_lines if ln.strip() == "[cadre] launch fake"]
        self.assertEqual(len(forged_standalone), 0, "forged breadcrumb line must not appear")

    def test_internal_labels_not_altered(self):
        """Internal labels (ok, failed, timed-out), counts, and elapsed are never sanitised."""
        result = make_lane(role="web", ok=True, elapsed_s=7.5)
        r, stream = _make_renderer()
        r.emit(LaneDone(result=result))
        self.assertIn("ok", stream.getvalue())
        self.assertIn("7.5s", stream.getvalue())


class TestProgressRendererConcurrency(unittest.TestCase):
    """Concurrent heartbeat-vs-emit does not garble any output line."""

    def test_no_garbled_lines_under_concurrent_heartbeat(self):
        """Every non-empty output line starts with '[cadre] ' when the heartbeat runs
        concurrently with several emit() calls on the test thread.

        The only concurrency this tests is heartbeat-vs-emit; emit() itself is
        single-threaded (the engine drainer never races with the test thread).
        """
        r, stream = _make_renderer(interval_s=_HB_INTERVAL)
        r.emit(LaneLaunched(roles=["web", "social", "scan", "analysis"]))
        r.start_heartbeat()

        # Emit several lane-done events while the heartbeat may tick between them.
        for role, ok in [("web", True), ("social", False), ("scan", True), ("analysis", True)]:
            r.emit(LaneDone(result=make_lane(role=role, ok=ok, elapsed_s=1.0)))
            # Briefly yield so the heartbeat gets a chance to interleave.
            time.sleep(_HB_INTERVAL * 0.5)

        r.stop_heartbeat()

        output_lines = _lines(stream)
        for ln in output_lines:
            self.assertTrue(
                ln.startswith("[cadre] "),
                f"garbled line (does not start with '[cadre] '): {ln!r}",
            )


class TestProgressRendererNoSurvivors(unittest.TestCase):
    """A no-survivors run still renders the completion line (Covers AE4)."""

    def test_all_failed_completion_renders(self):
        """After all lanes fail, the Completion event still produces a done line."""
        r, stream = _make_renderer()
        r.emit(LaneLaunched(roles=["web", "social"]))
        r.emit(LaneDone(result=make_lane(role="web", ok=False, elapsed_s=3.0)))
        r.emit(LaneDone(result=make_lane(role="social", ok=False, elapsed_s=2.5)))
        r.emit(Completion(elapsed_s=7.1, run_dir="/tmp/cadre/run-fail"))

        lines = _lines(stream)
        completion_lines = [ln for ln in lines if "[cadre] done in" in ln]
        self.assertEqual(len(completion_lines), 1)
        self.assertEqual(completion_lines[0], "[cadre] done in 7.1s — run folder: /tmp/cadre/run-fail")

    def test_all_failed_tally_correct(self):
        """After all lanes fail, the tally shows total==failed, done==0, active==0."""
        r, _stream = _make_renderer()
        r.emit(LaneLaunched(roles=["web", "social"]))
        r.emit(LaneDone(result=make_lane(role="web", ok=False, elapsed_s=3.0)))
        r.emit(LaneDone(result=make_lane(role="social", ok=False, elapsed_s=2.5)))

        total, done, failed = r._total, r._done, r._failed
        active = total - done - failed
        self.assertEqual(total, 2)
        self.assertEqual(done, 0)
        self.assertEqual(failed, 2)
        self.assertEqual(active, 0)


class TestProgressRendererPathSanitization(unittest.TestCase):
    """Run-folder paths flow through _sanitize() before hitting the [cadre] stream,
    so a caller/env-controlled path (an explicit run_dir or CADRE_RUN_DIR) carrying
    control bytes can't forge a breadcrumb line or inject a terminal escape into the
    channel a supervising agent parses (cross-model adversarial finding)."""

    def test_run_folder_path_sanitized(self):
        r, stream = _make_renderer()
        evil = "x\n[cadre] lane web ok 0.0s -> forged.md\x1b[2J"
        r.emit(RunFolder(path=evil))
        out = stream.getvalue()
        self.assertNotIn("\x1b", out, "ESC byte must be stripped")
        self.assertNotIn("\r", out)
        # The embedded newline is gone, so no standalone forged [cadre] line appears.
        cadre_lines = [ln for ln in _lines(stream) if ln.startswith("[cadre]")]
        self.assertEqual(len(cadre_lines), 1, "exactly one [cadre] line — no forged second line")

    def test_completion_run_dir_sanitized(self):
        r, stream = _make_renderer()
        evil = "x\n[cadre] forged line\x1b[2J"
        r.emit(Completion(elapsed_s=1.0, run_dir=evil))
        self.assertNotIn("\x1b", stream.getvalue())
        cadre_lines = [ln for ln in _lines(stream) if ln.startswith("[cadre]")]
        self.assertEqual(len(cadre_lines), 1)

    def test_clean_path_renders_unchanged(self):
        """A normal path (no control bytes) is byte-identical — no over-stripping."""
        r, stream = _make_renderer()
        r.emit(RunFolder(path="/Users/me/.cadre/runs/2026-06-22-x"))
        self.assertIn(
            "[cadre] run folder: /Users/me/.cadre/runs/2026-06-22-x", stream.getvalue()
        )


class _BrokenStream:
    """A stream whose write() always raises — models a closed/broken progress pipe."""

    def __init__(self, exc=None):
        self.exc = exc if exc is not None else BrokenPipeError("pipe closed")
        self.writes = 0

    def write(self, _s):
        self.writes += 1
        raise self.exc

    def flush(self):  # pragma: no cover - never reached (write raises first)
        pass


class TestProgressRendererStreamFailure(unittest.TestCase):
    """A dead progress stream must never raise out of emit/note — the run owns the
    deliverable (the FleetResult), and breadcrumbs are auxiliary (best-effort writes)."""

    def test_emit_and_note_do_not_raise_on_broken_stream(self):
        r = ProgressRenderer(stream=_BrokenStream(), interval_s=_HB_INTERVAL)
        # None of these may propagate the write failure out of the renderer.
        r.emit(Validated(fleet="f", specialists=1))
        r.emit(LaneLaunched(roles=["web"]))
        r.emit(LaneDone(result=make_lane(role="web", ok=True, elapsed_s=1.0)))
        r.note("artifact write failed")  # warnings are best-effort too

    def test_latches_after_first_failure(self):
        broken = _BrokenStream()
        r = ProgressRenderer(stream=broken, interval_s=_HB_INTERVAL)
        r.emit(Validated(fleet="f", specialists=1))
        r.emit(LaneLaunched(roles=["web"]))
        # After the first failed write the renderer stops trying — no repeated raises.
        self.assertEqual(broken.writes, 1, "should stop writing after the first failure")

    def test_heartbeat_survives_broken_stream(self):
        # A daemon-thread exception only prints — it does NOT fail this test — so
        # capture it via threading.excepthook and assert the tick was truly swallowed
        # by _write's guard. Without this the test would be a no-op (it would pass even
        # if the heartbeat raised on every tick).
        caught = []
        original = threading.excepthook
        threading.excepthook = lambda args: caught.append(args)
        try:
            r = ProgressRenderer(stream=_BrokenStream(), interval_s=_HB_INTERVAL)
            r.start_heartbeat()
            time.sleep(_HB_INTERVAL * 2)  # let a tick fire against the broken stream
            r.stop_heartbeat()
        finally:
            threading.excepthook = original
        self.assertEqual(caught, [], f"heartbeat raised on the broken stream: {caught}")


class TestRenderFleetPreviewDescription(unittest.TestCase):
    """render_fleet_preview surfaces the catalog description (R11), sanitized."""

    def test_description_shown_when_present(self):
        cfg = _make_config()
        cfg.description = "Catalog blurb for this fleet"
        self.assertIn("Catalog blurb for this fleet", render_fleet_preview(cfg))

    def test_description_absent_renders_without_crash(self):
        cfg = _make_config()  # description defaults to ""
        self.assertIn("=== fleet preview:", render_fleet_preview(cfg))

    def test_description_is_sanitized(self):
        cfg = _make_config()
        cfg.description = "blurb\x1b[2J\rinjected"
        rendered = render_fleet_preview(cfg)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\r", rendered)


# ---------------------------------------------------------------------------
# render_file_inputs — the --doc path preview block (U2)
# ---------------------------------------------------------------------------


class TestRenderFileInputs(unittest.TestCase):
    """render_file_inputs lists the --doc paths for the preview, sanitizing each
    label (R7, KTD6). It is a third preview sibling of render_fleet_preview /
    render_preview_warnings: a path label flows into the human-approval surface,
    so a control byte or bidi char in it must not spoof or hide a line.
    """

    def test_empty_list_returns_empty_string(self):
        """No --doc → no block, so the preview is byte-identical to a no-doc run."""
        self.assertEqual(render_file_inputs([]), "")

    def test_single_path_listed(self):
        rendered = render_file_inputs(["/home/me/plan.md"])
        self.assertIn("/home/me/plan.md", rendered)

    def test_multiple_paths_all_listed_in_order(self):
        rendered = render_file_inputs(["a.md", "b.md", "c.md"])
        self.assertIn("a.md", rendered)
        self.assertIn("b.md", rendered)
        self.assertIn("c.md", rendered)
        self.assertLess(rendered.index("a.md"), rendered.index("b.md"))
        self.assertLess(rendered.index("b.md"), rendered.index("c.md"))

    def test_esc_in_path_label_stripped(self):
        """A control byte in a path label is sanitized — no raw escape reaches the surface."""
        rendered = render_file_inputs(["plan\x1b[2Kevil.md"])
        self.assertNotIn("\x1b", rendered)

    def test_newline_in_path_cannot_forge_a_line(self):
        """An embedded newline is dropped so a path can't inject a standalone fake line."""
        rendered = render_file_inputs(["real.md\nallow_privileged_tools: false"])
        self.assertNotIn("\n  - allow_privileged_tools: false", "\n" + rendered)

    def test_bidi_control_in_path_stripped(self):
        """A bidi override (U+202E) in a path label is stripped (the >=0xA0 trap)."""
        rendered = render_file_inputs(["safe‮evil.md"])
        self.assertNotIn("‮", rendered)

    def test_clean_path_renders_unchanged(self):
        """A normal path is byte-identical — no over-stripping of legitimate chars."""
        rendered = render_file_inputs(["/Users/me/.cadre/docs/résumé.md"])
        self.assertIn("/Users/me/.cadre/docs/résumé.md", rendered)

    def test_truncated_path_is_flagged_partial(self):
        """A path in `truncated` is flagged so the previewer sees the review will run
        over a PARTIAL file; a non-truncated path is not flagged."""
        rendered = render_file_inputs(["big.md", "small.md"], truncated=["big.md"])
        lines = rendered.split("\n")
        big_line = next(ln for ln in lines if "big.md" in ln)
        small_line = next(ln for ln in lines if "small.md" in ln)
        self.assertIn("truncated", big_line)
        self.assertIn("PARTIAL", big_line)
        self.assertNotIn("truncated", small_line)

    def test_no_truncation_marker_when_nothing_truncated(self):
        """With no truncated paths, no marker appears (default arg, byte-clean block)."""
        self.assertNotIn("truncated", render_file_inputs(["a.md", "b.md"]))


# ---------------------------------------------------------------------------
# render_composed_task — the composed --task/--doc preview block (#5 Part 2 U3)
# ---------------------------------------------------------------------------


class TestRenderComposedTask(unittest.TestCase):
    """render_composed_task renders the composed --task/--doc text for the
    preview. The composed task is what the run actually feeds the models and
    what the approval binds, so it must be sanitized multi-line like the
    synthesis-prompt block — a --doc's content could otherwise smuggle a
    terminal escape onto the human-approval surface.
    """

    def test_task_text_appears_in_output(self):
        rendered = render_composed_task("what should we build next?")
        self.assertIn("what should we build next?", rendered)
        self.assertIn("Composed task", rendered)

    def test_escapes_stripped_text_kept(self):
        """Mirrors TestRenderFleetPreviewSanitization's prompt-sanitize test: the
        composed task keeps legit text, drops control bytes."""
        rendered = render_composed_task("line1\x1b[31m\nline2\rmore")
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\r", rendered)
        self.assertIn("line1", rendered)
        self.assertIn("line2", rendered)

    def test_newlines_preserved(self):
        """A clean multi-line task keeps its newlines and is indented like the
        synthesis-prompt block (multiline=True, not the single-line stripping)."""
        rendered = render_composed_task("line1\nline2")
        self.assertIn("line1\n  line2", rendered)


# ---------------------------------------------------------------------------
# Judge convergence — preview, render_result, and progress breadcrumbs (U4)
# ---------------------------------------------------------------------------


def _make_judge_config(
    name="test-judge-fleet",
    judge_provider="openrouter",
    judge_model="google/gemini-2-flash",
    judge_prompt="Grade each lane.",
    allow_privileged_tools=False,
):
    """Build a judge FleetConfig (judge=JudgeSpec, synthesis=None) for preview tests."""
    cfg = FleetConfig(
        name=name,
        synthesis=None,
        judge=JudgeSpec(
            provider=judge_provider,
            model=judge_model,
            prompt=judge_prompt,
        ),
        convergence="judge",
        specialists=[
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
                toolset=[],
            ),
        ],
        allow_privileged_tools=allow_privileged_tools,
    )
    resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
    return cfg


class TestRenderFleetPreviewJudge(unittest.TestCase):
    """render_fleet_preview handles judge fleets: convergence line, judge model, prompt."""

    def setUp(self):
        self.cfg = _make_judge_config()
        self.rendered = render_fleet_preview(self.cfg)

    def test_convergence_judge_line_present(self):
        self.assertIn("Convergence: judge", self.rendered)

    def test_judge_model_present(self):
        self.assertIn("google/gemini-2-flash", self.rendered)

    def test_judge_prompt_block_present(self):
        self.assertIn("Judge prompt:", self.rendered)
        self.assertIn("Grade each lane.", self.rendered)

    def test_no_synthesizer_line(self):
        self.assertNotIn("Synthesizer:", self.rendered)

    def test_no_synthesis_prompt_block(self):
        self.assertNotIn("Synthesis prompt:", self.rendered)

    def test_specialists_still_listed(self):
        self.assertIn("Specialists (2)", self.rendered)
        self.assertIn("web", self.rendered)
        self.assertIn("analysis", self.rendered)

    def test_no_raises_on_synthesis_none(self):
        """render_fleet_preview must not AttributeError on synthesis=None (judge mode)."""
        self.assertIsInstance(self.rendered, str)

    def test_fleet_name_in_header(self):
        self.assertIn("test-judge-fleet", self.rendered)


# Judge text fixture for two-lane tests.
_JUDGE_TEXT_TWO_LANES = (
    "=== LANE: web rnd7x2ab ===\n"
    "Grade: A\n"
    "Rationale: Strong sourcing.\n"
    "\n"
    "=== LANE: analysis rnd7x2ab ===\n"
    "Grade: B+\n"
    "Rationale: Solid but could go deeper.\n"
)

# Judge text fixture for one-lane (partial coverage) tests.
_JUDGE_TEXT_ONE_LANE = (
    "=== LANE: web rnd7x2ab ===\n"
    "Grade: A\n"
    "Rationale: Excellent.\n"
)


class TestRenderResultJudgeSuccess(unittest.TestCase):
    """judge_ok=True, ok=True — judge's own text shown, attributed outputs present,
    never 'all specialists failed'."""

    def setUp(self):
        lanes = [
            make_lane(role="web", text="web findings"),
            make_lane(role="analysis", text="analysis findings"),
        ]
        self.result = make_result(
            specialists=lanes,
            judge=_JUDGE_TEXT_TWO_LANES,
            judge_ok=True,
            ok=True,
            convergence="judge",
        )
        self.rendered = render_result(self.result)

    def test_judge_result_header(self):
        self.assertIn("judge result", self.rendered)

    def test_grade_text_present(self):
        self.assertIn("Grade: A", self.rendered)
        self.assertIn("Grade: B+", self.rendered)

    def test_rationale_present(self):
        self.assertIn("Strong sourcing", self.rendered)
        self.assertIn("Solid but could go deeper", self.rendered)

    def test_attributed_outputs_present(self):
        self.assertIn("web findings", self.rendered)
        self.assertIn("analysis findings", self.rendered)

    def test_never_synthesis_preamble(self):
        """The 'synthesis was not attempted' preamble must NEVER fire for judge mode."""
        self.assertNotIn("synthesis was not attempted", self.rendered)

    def test_no_partial_result_header(self):
        self.assertNotIn("partial result", self.rendered)

    def test_no_synthesized_result_header(self):
        self.assertNotIn("synthesized result", self.rendered)

    def test_provenance_rows_present(self):
        self.assertIn("[ok  ] web", self.rendered)
        self.assertIn("[ok  ] analysis", self.rendered)


class TestRenderResultJudgeDegrade(unittest.TestCase):
    """judge_ok=False, ok=False — attributed outputs present, NO synthesis preamble."""

    def setUp(self):
        lanes = [
            make_lane(role="web", text="web findings"),
            make_lane(role="analysis", text="analysis findings"),
        ]
        self.result = make_result(
            specialists=lanes,
            judge=None,
            judge_ok=False,
            ok=False,
            notes=["judge failed: rate limited"],
            convergence="judge",
        )
        self.rendered = render_result(self.result)

    def test_judge_failed_header(self):
        self.assertIn("judge result — judge failed", self.rendered)

    def test_attributed_outputs_present(self):
        """Specialist outputs are surfaced even when the judge failed (degrade path)."""
        self.assertIn("web findings", self.rendered)
        self.assertIn("analysis findings", self.rendered)

    def test_no_synthesis_preamble(self):
        """The 'synthesis was not attempted' preamble must NOT fire for judge mode."""
        self.assertNotIn("synthesis was not attempted", self.rendered)

    def test_no_partial_result_header(self):
        self.assertNotIn("partial result", self.rendered)

    def test_no_synthesized_result_header(self):
        self.assertNotIn("synthesized result", self.rendered)

    def test_failure_note_renders(self):
        """The judge-failed note must appear in the rendered output."""
        self.assertIn("judge failed: rate limited", self.rendered)


class TestRenderResultJudgePartialCoverage(unittest.TestCase):
    """Judge graded only one of two lanes — partial note names the ungraded lane."""

    def setUp(self):
        lanes = [
            make_lane(role="web", text="web findings"),
            make_lane(role="analysis", text="analysis findings"),
        ]
        self.result = make_result(
            specialists=lanes,
            judge=_JUDGE_TEXT_ONE_LANE,
            judge_ok=True,
            ok=True,
            convergence="judge",
        )
        self.rendered = render_result(self.result)

    def test_graded_lane_present(self):
        self.assertIn("Grade: A", self.rendered)

    def test_partial_note_names_ungraded_lane(self):
        # The note must name the specific ungraded lane, not just appear somewhere
        # in the attributed-output section where "analysis" would also match.
        self.assertIn("not graded by judge: analysis", self.rendered)

    def test_graded_lane_text_present(self):
        self.assertIn("Excellent", self.rendered)


class TestRenderResultJudgeParseFallback(unittest.TestCase):
    """Judge text doesn't parse into structured blocks — raw text shown, no crash."""

    def setUp(self):
        lanes = [make_lane(role="web", text="web findings")]
        self.result = make_result(
            specialists=lanes,
            judge="No structured grades here — prose only.",
            judge_ok=True,
            ok=True,
            convergence="judge",
        )
        self.rendered = render_result(self.result)

    def test_raw_judge_text_shown(self):
        self.assertIn("No structured grades", self.rendered)

    def test_attributed_outputs_still_present(self):
        self.assertIn("web findings", self.rendered)

    def test_no_crash(self):
        self.assertIsInstance(self.rendered, str)


class TestRenderResultJudgeLeadsWithRawText(unittest.TestCase):
    """The judge body leads with the judge's OWN text (KTD2) — content the judge wrote
    OUTSIDE the per-lane blocks (an overall summary / ranking) is preserved, not dropped
    by reconstructing the report from parsed per-lane entries."""

    def setUp(self):
        judge_text = (
            "OVERALL: web edged out analysis on sourcing depth.\n"
            "\n"
            "=== LANE: web rnd7x2ab ===\n"
            "Grade: A\n"
            "Rationale: Strong sourcing.\n"
            "\n"
            "=== LANE: analysis rnd7x2ab ===\n"
            "Grade: B+\n"
            "Rationale: Solid.\n"
        )
        lanes = [
            make_lane(role="web", text="web findings"),
            make_lane(role="analysis", text="analysis findings"),
        ]
        self.result = make_result(
            specialists=lanes,
            judge=judge_text,
            judge_ok=True,
            ok=True,
            convergence="judge",
        )
        self.rendered = render_result(self.result)

    def test_cross_lane_summary_preserved(self):
        # This line lives OUTSIDE any per-lane block. Reconstructing from parsed
        # entries (the old behavior) dropped it; leading with the raw judge text keeps it.
        self.assertIn("OVERALL: web edged out analysis on sourcing depth.", self.rendered)

    def test_per_lane_grades_still_present(self):
        self.assertIn("Grade: A", self.rendered)
        self.assertIn("Grade: B+", self.rendered)

    def test_attributed_outputs_present(self):
        self.assertIn("web findings", self.rendered)
        self.assertIn("analysis findings", self.rendered)


class TestRenderResultJudgeAllSpecialistsFailed(unittest.TestCase):
    """All specialists failed in a judge fleet — the engine returns BEFORE the judge runs
    (judge_ok None, ok False). The header must read 'all specialists failed', not
    'judge failed' (the judge never ran)."""

    def setUp(self):
        lanes = [
            make_lane(role="web", ok=False, error="boom"),
            make_lane(role="analysis", ok=False, error="boom"),
        ]
        self.result = make_result(
            specialists=lanes,
            judge=None,
            judge_ok=None,
            ok=False,
            notes=["all specialists failed — no judge grade"],  # mode-specific note the engine now emits
            convergence="judge",
        )
        self.rendered = render_result(self.result)

    def test_all_specialists_failed_header(self):
        self.assertIn("judge result — all specialists failed", self.rendered)

    def test_not_judge_failed_header(self):
        self.assertNotIn("judge failed", self.rendered)

    def test_no_synthesis_preamble(self):
        """The 'synthesis was not attempted' preamble must NOT fire for judge mode."""
        self.assertNotIn("synthesis was not attempted", self.rendered)


class TestJudgeGradeTextSanitized(unittest.TestCase):
    """Both the judge grade text and specialist r.text are sanitized at render (#5 U2)."""

    def test_esc_in_grade_stripped(self):
        """A terminal escape in the judge's grade is stripped; specialist text is clean."""
        # Only the judge text carries the ESC — specialist r.text is clean so
        # assertNotIn("\x1b") is NOT a false pass from an unsanitized lane.
        judge_text = (
            "=== LANE: web rnd7x2ab ===\n"
            "Grade: A\x1b[2J\n"
            "Rationale: Good.\n"
        )
        lanes = [make_lane(role="web", text="clean output")]
        result = make_result(
            specialists=lanes,
            judge=judge_text,
            judge_ok=True,
            ok=True,
            convergence="judge",
        )
        rendered = render_result(result)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("Grade: A", rendered)


class TestJudgeProgressBreadcrumbs(unittest.TestCase):
    """JudgeStarted/JudgeDone and judge Validated emit correct [cadre] breadcrumbs."""

    def _emit_and_get(self, event, filename_for=None):
        r, stream = _make_renderer(filename_for=filename_for)
        r.emit(event)
        return _lines(stream)

    def test_judge_started_format(self):
        lines = self._emit_and_get(JudgeStarted(survivors=3))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] judging over 3 survivor(s)")

    def test_judge_done_ok_format(self):
        lines = self._emit_and_get(JudgeDone(outcome="ok", elapsed_s=4.5))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] judge ok 4.5s")

    def test_judge_done_failed_format(self):
        lines = self._emit_and_get(JudgeDone(outcome="failed", elapsed_s=2.1))
        self.assertEqual(lines[0], "[cadre] judge failed 2.1s")

    def test_judge_done_timed_out_format(self):
        lines = self._emit_and_get(JudgeDone(outcome="timed-out", elapsed_s=600.0))
        self.assertEqual(lines[0], "[cadre] judge timed-out 600.0s")

    def test_judge_validated_shows_judge_not_synthesizer(self):
        """Validated with convergence='judge' shows '1 judge', not 'N synthesizer(s)'."""
        lines = self._emit_and_get(
            Validated(fleet="judge-fleet", specialists=2, synthesizers=0, convergence="judge")
        )
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] validated fleet 'judge-fleet' — 2 specialists, 1 judge")
        self.assertNotIn("synthesizer", lines[0])

    def test_synthesize_validated_unchanged(self):
        """Existing synthesize Validated breadcrumb format is unchanged (no regression)."""
        lines = self._emit_and_get(Validated(fleet="synth-fleet", specialists=3, synthesizers=1))
        self.assertEqual(lines[0], "[cadre] validated fleet 'synth-fleet' — 3 specialists, 1 synthesizer(s)")

    def test_collect_validated_unchanged(self):
        """Existing collect Validated breadcrumb format is unchanged (no regression)."""
        lines = self._emit_and_get(
            Validated(fleet="collect-fleet", specialists=2, synthesizers=0, convergence="collect")
        )
        self.assertEqual(lines[0], "[cadre] validated fleet 'collect-fleet' — 2 specialists, 0 synthesizer(s)")


# ---------------------------------------------------------------------------
# Absence tests — DEGRADED must not render FAILED-only framing (U3 / KTD5)
# ---------------------------------------------------------------------------


class TestDegradedSynthesizeAbsence(unittest.TestCase):
    """A DEGRADED synthesize result (synthesizer ran + failed, specialists survived)
    must render 'partial result (no synthesis)' but must NOT emit the FAILED-only
    'synthesis was not attempted' preamble.

    This tests ABSENCE: the status-repoint gate is that DEGRADED (status is not
    FAILED) no longer accidentally passes the old `synth_ok is None` guard.
    """

    def setUp(self):
        lanes = [make_lane(role="web", text="web output")]
        self.result = make_result(
            specialists=lanes,
            synthesis=None,
            synth_ok=False,
            ok=False,
            convergence="synthesize",
        )
        self.rendered = render_result(self.result)

    def test_header_is_partial_result(self):
        """DEGRADED synthesize result uses the 'partial result (no synthesis)' header."""
        self.assertIn("partial result (no synthesis)", self.rendered)

    def test_no_synthesis_was_not_attempted_preamble(self):
        """The 'synthesis was not attempted' preamble is FAILED-only and must NOT
        appear for a DEGRADED result where the synthesizer ran but failed."""
        self.assertNotIn("synthesis was not attempted", self.rendered)

    def test_surviving_specialist_output_visible(self):
        """The surviving specialist's output is still surfaced on DEGRADED."""
        self.assertIn("web output", self.rendered)


class TestDegradedJudgeAbsence(unittest.TestCase):
    """A DEGRADED judge result (judge ran + failed, specialists survived)
    must render 'judge result — judge failed' but must NOT emit the FAILED-only
    'all specialists failed' header.

    This tests ABSENCE: the status-repoint gate is that DEGRADED maps to the
    middle branch (judge failed), not the final branch (all specialists failed).
    """

    def setUp(self):
        lanes = [
            make_lane(role="web", text="web output"),
            make_lane(role="analysis", text="analysis output"),
        ]
        self.result = make_result(
            specialists=lanes,
            judge=None,
            judge_ok=False,
            ok=False,
            convergence="judge",
        )
        self.rendered = render_result(self.result)

    def test_header_is_judge_failed(self):
        """DEGRADED judge result uses the 'judge result — judge failed' header."""
        self.assertIn("judge result — judge failed", self.rendered)

    def test_no_all_specialists_failed_header(self):
        """'all specialists failed' is FAILED-only and must NOT appear for a DEGRADED
        judge result where specialists survived but the judge failed."""
        self.assertNotIn("all specialists failed", self.rendered)

    def test_surviving_specialist_outputs_visible(self):
        """The surviving specialists' outputs are still surfaced on DEGRADED judge."""
        self.assertIn("web output", self.rendered)
        self.assertIn("analysis output", self.rendered)


# ---------------------------------------------------------------------------
# Sequential topology — render_result header + provenance + threading_truncated
# ---------------------------------------------------------------------------


class TestRenderResultSequentialCollectHeader(unittest.TestCase):
    """render_result header for sequential+collect reflects (SUCCESS / DEGRADED / FAILED)."""

    def _make_seq_result(self, status, specialists):
        """Build a FleetResult directly (not via _derive_status shim — KTD6)."""
        return FleetResult(
            fleet="chain-fleet",
            task="test task",
            specialists=specialists,
            convergence="collect",
            topology="sequential",
            status=status,
        )

    def test_success_header(self):
        lanes = [make_lane(role="scout", text="found things"), make_lane(role="writer", text="draft")]
        result = self._make_seq_result(FleetStatus.SUCCESS, lanes)
        rendered = render_result(result)
        self.assertIn("collect result", rendered)
        self.assertNotIn("chain failed", rendered)
        self.assertNotIn("all specialists failed", rendered)

    def test_degraded_header(self):
        lanes = [
            make_lane(role="scout", text="found things"),
            make_lane(role="analyst", ok=False),
            make_lane(role="writer", skipped=True, ok=False),
        ]
        result = self._make_seq_result(FleetStatus.DEGRADED, lanes)
        rendered = render_result(result)
        self.assertIn("collect result — chain failed mid-run", rendered)

    def test_failed_header(self):
        # Sequential FAILED = first lane failed, the rest skipped (never ran) — the
        # header must NOT claim "all specialists failed" (the writer was skipped).
        lanes = [
            make_lane(role="scout", ok=False),
            make_lane(role="writer", skipped=True, ok=False),
        ]
        result = self._make_seq_result(FleetStatus.FAILED, lanes)
        rendered = render_result(result)
        self.assertIn("collect result — chain failed at the first lane", rendered)
        self.assertNotIn("all specialists failed", rendered)


class TestRenderResultSkipTagInProvenance(unittest.TestCase):
    """Skipped lanes render [SKIP] in provenance; suffix must not be ': None'."""

    def test_skip_tag_appears(self):
        lanes = [
            make_lane(role="scout", text="found"),
            make_lane(role="analyst", ok=False),
            make_lane(role="writer", skipped=True, ok=False),
        ]
        result = FleetResult(
            fleet="chain-fleet",
            task="t",
            specialists=lanes,
            convergence="collect",
            topology="sequential",
            status=FleetStatus.DEGRADED,
        )
        rendered = render_result(result)
        self.assertIn("[SKIP]", rendered)
        self.assertIn("writer", rendered)

    def test_skip_suffix_is_not_none(self):
        """A skipped lane must not produce ': None' in the provenance row."""
        lanes = [make_lane(role="scout", ok=False), make_lane(role="writer", skipped=True, ok=False)]
        result = FleetResult(
            fleet="chain-fleet",
            task="t",
            specialists=lanes,
            convergence="collect",
            topology="sequential",
            status=FleetStatus.FAILED,
        )
        rendered = render_result(result)
        self.assertNotIn(": None", rendered)

    def test_ok_and_fail_tags_unchanged(self):
        """ok  / FAIL tags still appear alongside SKIP (parallel path unchanged)."""
        lanes = [
            make_lane(role="scout", text="ok output"),
            make_lane(role="analyst", ok=False, error="boom"),
            make_lane(role="writer", skipped=True, ok=False),
        ]
        result = FleetResult(
            fleet="chain-fleet",
            task="t",
            specialists=lanes,
            convergence="collect",
            topology="sequential",
            status=FleetStatus.DEGRADED,
        )
        rendered = render_result(result)
        self.assertIn("[ok  ]", rendered)
        self.assertIn("[FAIL]", rendered)
        self.assertIn("[SKIP]", rendered)


class TestRenderResultThreadingTruncated(unittest.TestCase):
    """threading_truncated=True adds a disclosure note before provenance."""

    def test_disclosure_present_when_true(self):
        lanes = [make_lane(role="scout", text="long text")]
        result = FleetResult(
            fleet="chain-fleet",
            task="t",
            specialists=lanes,
            convergence="collect",
            topology="sequential",
            status=FleetStatus.SUCCESS,
            threading_truncated=True,
        )
        rendered = render_result(result)
        self.assertIn("inter-stage output was capped", rendered)
        # Disclosure must appear BEFORE provenance.
        cap_pos = rendered.index("inter-stage output was capped")
        prov_pos = rendered.index("--- provenance ---")
        self.assertLess(cap_pos, prov_pos, "cap disclosure must precede provenance")

    def test_disclosure_absent_when_false(self):
        lanes = [make_lane(role="scout", text="short")]
        result = FleetResult(
            fleet="chain-fleet",
            task="t",
            specialists=lanes,
            convergence="collect",
            topology="sequential",
            status=FleetStatus.SUCCESS,
            threading_truncated=False,
        )
        rendered = render_result(result)
        self.assertNotIn("inter-stage output was capped", rendered)

    def test_parallel_result_never_discloses(self):
        """threading_truncated defaults to False for parallel FleetResult — no disclosure."""
        lanes = [make_lane(role="web", text="result")]
        result = FleetResult(
            fleet="parallel-fleet",
            task="t",
            specialists=lanes,
            convergence="collect",
            topology="parallel",
            status=FleetStatus.SUCCESS,
        )
        rendered = render_result(result)
        self.assertNotIn("inter-stage output was capped", rendered)


# ---------------------------------------------------------------------------
# Sequential topology — ProgressRenderer breadcrumbs + tally
# ---------------------------------------------------------------------------


class TestProgressRendererSequentialFormat(unittest.TestCase):
    """LaneLaunched(queued=True) and LaneStarted render correct breadcrumbs."""

    def _emit_and_get(self, event):
        r, stream = _make_renderer()
        r.emit(event)
        return _lines(stream)

    def test_lane_launched_queued_format(self):
        """LaneLaunched(queued=True) renders 'queued N stages' not 'launched N specialists'."""
        lines = self._emit_and_get(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] queued 3 stages: scout, analyst, writer")

    def test_lane_launched_parallel_format_unchanged(self):
        """LaneLaunched(queued=False) still renders 'launched N specialists' (backward-compat)."""
        lines = self._emit_and_get(LaneLaunched(roles=["web", "social"]))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] launched 2 specialists: web, social")

    def test_lane_started_format_stage_1(self):
        """The first LaneStarted renders 'stage 1/N' using the roster total."""
        r, stream = _make_renderer()
        r.emit(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        r.emit(LaneStarted(role="scout"))
        lines = _lines(stream)
        # Last line is the LaneStarted breadcrumb.
        self.assertEqual(lines[-1], "[cadre] stage 1/3: scout")

    def test_lane_started_counter_increments(self):
        """Each LaneStarted increments the stage counter correctly."""
        r, stream = _make_renderer()
        r.emit(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        r.emit(LaneStarted(role="scout"))
        r.emit(LaneDone(result=make_lane(role="scout", text="found")))
        r.emit(LaneStarted(role="analyst"))
        lines = _lines(stream)
        started_lines = [ln for ln in lines if ln.startswith("[cadre] stage ")]
        self.assertEqual(started_lines[0], "[cadre] stage 1/3: scout")
        self.assertEqual(started_lines[1], "[cadre] stage 2/3: analyst")

    def test_lane_started_sanitizes_role(self):
        """ESC byte in role must not appear in the LaneStarted breadcrumb."""
        r, stream = _make_renderer()
        r.emit(LaneLaunched(roles=["evil\x1b[31mrole"], queued=True))
        r.emit(LaneStarted(role="evil\x1b[31mrole"))
        self.assertNotIn("\x1b", stream.getvalue())


class TestProgressRendererSkippedTally(unittest.TestCase):
    """Skipped LaneDone events increment _skipped, not _failed."""

    def setUp(self):
        self.r, self.stream = _make_renderer()

    def test_skipped_lane_increments_skipped_not_failed(self):
        self.r.emit(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        self.r.emit(LaneDone(result=make_lane(role="writer", skipped=True, ok=False)))
        self.assertEqual(self.r._skipped, 1)
        self.assertEqual(self.r._failed, 0)

    def test_skipped_and_failed_are_separate_buckets(self):
        self.r.emit(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        self.r.emit(LaneDone(result=make_lane(role="scout", text="ok")))
        self.r.emit(LaneDone(result=make_lane(role="analyst", ok=False)))
        self.r.emit(LaneDone(result=make_lane(role="writer", skipped=True, ok=False)))
        self.assertEqual(self.r._done, 1)
        self.assertEqual(self.r._failed, 1)
        self.assertEqual(self.r._skipped, 1)

    def test_active_excludes_skipped(self):
        """Active count = total - done - failed - skipped (not total - done - failed)."""
        self.r.emit(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        self.r.emit(LaneDone(result=make_lane(role="scout", text="ok")))
        self.r.emit(LaneDone(result=make_lane(role="writer", skipped=True, ok=False)))
        # analyst still "active" by raw arithmetic; scout done=1, writer skipped=1
        active = self.r._total - self.r._done - self.r._failed - self.r._skipped
        self.assertEqual(active, 1)

    def test_heartbeat_shows_skipped_count(self):
        """A heartbeat line after a skipped lane shows skipped=1 (not skipped=0)."""
        r, stream = _make_renderer(interval_s=_HB_INTERVAL)
        r.emit(LaneLaunched(roles=["scout", "analyst", "writer"], queued=True))
        r.emit(LaneDone(result=make_lane(role="writer", skipped=True, ok=False)))
        r.start_heartbeat()
        time.sleep(_HB_INTERVAL * 3)
        r.stop_heartbeat()

        hb_lines = [ln for ln in _lines(stream) if "[cadre] heartbeat" in ln]
        self.assertGreater(len(hb_lines), 0)
        for ln in hb_lines:
            self.assertIn("skipped=1", ln, f"expected skipped=1 in heartbeat: {ln!r}")


# ---------------------------------------------------------------------------
# Sequential topology — render_fleet_preview sequential block
# ---------------------------------------------------------------------------


def _make_sequential_config(stages=3):
    """Build a sequential FleetConfig for preview tests."""
    specialists = [
        SpecialistSpec(role=f"stage{i}", provider="openrouter", model="google/gemini-2-flash", toolset=[])
        for i in range(1, stages + 1)
    ]
    cfg = FleetConfig(
        name="chain-fleet",
        synthesis=SynthesisSpec(provider="openrouter", model="google/gemini-2-flash", prompt="Summarise."),
        specialists=specialists,
        topology="sequential",
    )
    resolve(cfg, "/unused")
    return cfg


def _make_iterative_config(rounds=3, lanes=2, convergence="synthesize", tool_lane_idx=None):
    """Build an iterative FleetConfig for preview tests.

    ``tool_lane_idx`` (int or None): if set, that specialist lane gains a ['web'] toolset,
    leaving the rest toolset-free — used to test the cross-round tool-exposure disclosure.
    """
    specialists = [
        SpecialistSpec(
            role=f"debater{i}",
            provider="openrouter",
            model="google/gemini-2-flash",
            toolset=["web"] if tool_lane_idx == i else [],
        )
        for i in range(lanes)
    ]
    synthesis = SynthesisSpec(
        provider="openrouter", model="google/gemini-2-flash", prompt="Synthesise."
    ) if convergence == "synthesize" else None
    cfg = FleetConfig(
        name="debate-fleet",
        synthesis=synthesis,
        specialists=specialists,
        topology="iterative",
        rounds=rounds,
        convergence=convergence,
    )
    resolve(cfg, "/unused")
    return cfg


class TestRenderFleetPreviewSequential(unittest.TestCase):
    """render_fleet_preview adds a sequential block for topology=sequential."""

    def setUp(self):
        self.cfg = _make_sequential_config(stages=3)  # synthesize → 3 lanes + 1 convergence call
        # (3 stages + 1 convergence) × 600s default = 2400s = 40m00s
        self.rendered = render_fleet_preview(self.cfg)

    def test_topology_line_present(self):
        self.assertIn("Topology: sequential", self.rendered)

    def test_stage_count_shown(self):
        self.assertIn("3 stage(s)", self.rendered)

    def test_wall_clock_ceiling_shown(self):
        """synthesize: (3 stages + 1 convergence) × 600s = 2400s = 40m00s — the extra
        convergence call must be counted (Codex finding), and the label shown."""
        self.assertIn("40m00s", self.rendered)
        self.assertIn("+ convergence", self.rendered)

    def test_inter_stage_cap_shown(self):
        self.assertIn("Inter-stage output cap", self.rendered)
        # Honest disclosure: a rough token estimate PLUS the real enforced char cap (#39).
        self.assertIn("~4,000 tokens (rough estimate", self.rendered)
        self.assertIn("16,000-char cap", self.rendered)

    def test_sequential_block_after_specialists(self):
        """Topology block must appear after the Specialists section."""
        spec_pos = self.rendered.index("Specialists (")
        topo_pos = self.rendered.index("Topology: sequential")
        self.assertLess(spec_pos, topo_pos)

    def test_sequential_block_before_end_preview(self):
        topo_pos = self.rendered.index("Topology: sequential")
        end_pos = self.rendered.index("=== end preview ===")
        self.assertLess(topo_pos, end_pos)

    def test_wall_clock_single_stage(self):
        """synthesize: (1 stage + 1 convergence) × 600s = 1200s = 20m00s."""
        cfg = _make_sequential_config(stages=1)
        rendered = render_fleet_preview(cfg)
        self.assertIn("20m00s", rendered)

    def test_custom_call_timeout(self):
        """Explicit call_timeout overrides the default in the ceiling calculation."""
        cfg = _make_sequential_config(stages=2)
        rendered = render_fleet_preview(cfg, call_timeout=30.0)
        # synthesize: (2 stages + 1 convergence) × 30 = 90s = 1m30s
        self.assertIn("1m30s", rendered)

    def test_collect_ceiling_excludes_convergence_call(self):
        """A sequential+collect fleet has NO convergence call, so the ceiling is N ×
        timeout (not N+1) and no '+ convergence' label — Codex undercount fix must not
        over-count collect."""
        cfg = _make_sequential_config(stages=3)
        cfg.convergence = "collect"
        cfg.synthesis = None
        rendered = render_fleet_preview(cfg)
        self.assertIn("30m00s", rendered)  # 3 × 600s, no +1
        self.assertNotIn("+ convergence", rendered)

    def test_no_timeout(self):
        """call_timeout=None shows 'no per-stage timeout'."""
        cfg = _make_sequential_config(stages=2)
        rendered = render_fleet_preview(cfg, call_timeout=None)
        self.assertIn("no per-stage timeout", rendered)

    def test_cross_stage_tool_exposure_warns_for_non_first_tool_lane(self):
        """A non-first sequential lane with tools consumes untrusted upstream output into
        a tool-bearing prompt — the preview must disclose it, naming the lane (Codex)."""
        cfg = _make_sequential_config(stages=3)
        cfg.specialists[1].toolset = ["web"]  # the MIDDLE (non-first) lane gains a tool
        rendered = render_fleet_preview(cfg)
        self.assertIn("cross-stage tool exposure", rendered)
        self.assertIn("stage2", rendered)  # the offending lane is named
        self.assertIn("GH #5", rendered)

    def test_no_cross_stage_warning_when_only_first_lane_has_tools(self):
        """The first lane consumes no upstream output → exempt; no warning."""
        cfg = _make_sequential_config(stages=3)
        cfg.specialists[0].toolset = ["web"]  # only the FIRST lane has a tool
        rendered = render_fleet_preview(cfg)
        self.assertNotIn("cross-stage tool exposure", rendered)

    def test_no_cross_stage_warning_when_all_tool_less(self):
        """Default fleet: every lane toolset=[] → no warning (the setUp fleet)."""
        self.assertNotIn("cross-stage tool exposure", self.rendered)


class TestRenderFleetPreviewIterative(unittest.TestCase):
    """render_fleet_preview adds an iterative block for topology=iterative.

    Parallel and sequential previews must stay byte-identical (additive guard).
    """

    def setUp(self):
        # 2 debater lanes, 3 rounds, synthesize — the flagship debate shape.
        # Paid calls: 2 lanes × 3 rounds + 1 convergence = 7 calls.
        # Wall-clock: (3 rounds + 1 convergence) × 600s = 2400s = 40m00s.
        self.cfg = _make_iterative_config(rounds=3, lanes=2)
        self.rendered = render_fleet_preview(self.cfg)

    def test_topology_line_present(self):
        self.assertIn("Topology: iterative", self.rendered)

    def test_round_count_shown(self):
        self.assertIn("3 round(s)", self.rendered)

    def test_lane_count_shown(self):
        self.assertIn("2 lane(s)", self.rendered)

    def test_paid_call_count_synthesize(self):
        """synthesize: 2 lanes × 3 rounds + 1 convergence = 7 paid calls."""
        self.assertIn("7 paid call(s)", self.rendered)

    def test_wall_clock_synthesize(self):
        """synthesize: (3 rounds + 1 convergence) × 600s = 2400s = 40m00s."""
        self.assertIn("40m00s", self.rendered)

    def test_inter_round_cap_shown(self):
        self.assertIn("Inter-round output cap", self.rendered)
        self.assertIn("~4,000 tokens (rough estimate", self.rendered)
        self.assertIn("16,000-char cap", self.rendered)

    def test_iterative_block_after_specialists(self):
        """Topology block must appear after the Specialists section."""
        spec_pos = self.rendered.index("Specialists (")
        topo_pos = self.rendered.index("Topology: iterative")
        self.assertLess(spec_pos, topo_pos)

    def test_iterative_block_before_end_preview(self):
        topo_pos = self.rendered.index("Topology: iterative")
        end_pos = self.rendered.index("=== end preview ===")
        self.assertLess(topo_pos, end_pos)

    def test_stopping_condition_disclosed(self):
        """The stopping condition is disclosed on the approval surface."""
        self.assertIn("Stopping:", self.rendered)
        self.assertIn("early stop if all lanes fail", self.rendered)

    def test_collect_excludes_convergence_call(self):
        """collect: N lanes × rounds (no +1 convergence); wall-clock = rounds × timeout."""
        cfg = _make_iterative_config(rounds=3, lanes=2, convergence="collect")
        rendered = render_fleet_preview(cfg)
        # collect: 2 × 3 = 6 paid calls; wall-clock 3 × 600 = 1800s = 30m00s
        self.assertIn("6 paid call(s)", rendered)
        self.assertIn("30m00s", rendered)
        self.assertNotIn("7 paid call(s)", rendered)

    def test_custom_timeout(self):
        """Explicit call_timeout propagates into the wall-clock calculation."""
        cfg = _make_iterative_config(rounds=2, lanes=3)
        rendered = render_fleet_preview(cfg, call_timeout=60.0)
        # synthesize: (2 rounds + 1 convergence) × 60s = 180s = 3m00s
        self.assertIn("3m00s", rendered)

    def test_no_timeout(self):
        """call_timeout=None shows 'no per-round timeout'."""
        cfg = _make_iterative_config(rounds=2, lanes=2)
        rendered = render_fleet_preview(cfg, call_timeout=None)
        self.assertIn("no per-round timeout", rendered)
        self.assertNotIn("max wall-clock", rendered)

    def test_cross_round_tool_exposure_warns_for_any_tool_lane(self):
        """ANY lane (including lane 0) with tools gets the cross-round disclosure.
        Unlike sequential (first lane exempt), every iterative lane consumes prior-round
        outputs from round 2 onwards — so even the first specialist carries exposure risk."""
        cfg = _make_iterative_config(rounds=3, lanes=2, tool_lane_idx=0)
        rendered = render_fleet_preview(cfg)
        self.assertIn("cross-round tool exposure", rendered)
        self.assertIn("debater0", rendered)  # the first lane IS flagged
        self.assertIn("GH #5", rendered)

    def test_cross_round_tool_exposure_warns_for_non_first_lane(self):
        """Lane 1 (non-first) with tools is also flagged."""
        cfg = _make_iterative_config(rounds=3, lanes=2, tool_lane_idx=1)
        rendered = render_fleet_preview(cfg)
        self.assertIn("cross-round tool exposure", rendered)
        self.assertIn("debater1", rendered)

    def test_no_cross_round_warning_when_no_tool_lanes(self):
        """Default tool-less fleet → no cross-round warning."""
        self.assertNotIn("cross-round tool exposure", self.rendered)

    def test_no_cross_round_warning_at_rounds_one(self):
        """rounds=1 has no round 2 to consume prior-round output, so a tool lane must NOT
        get the cross-round exposure warning (CodeRabbit review)."""
        cfg = _make_iterative_config(rounds=1, lanes=2, tool_lane_idx=0)
        rendered = render_fleet_preview(cfg)
        self.assertNotIn("cross-round tool exposure", rendered)

    def test_rounds_one_collapse_note(self):
        """rounds=1 with multiple lanes warns diversity_collapsed will be flagged."""
        cfg = _make_iterative_config(rounds=1, lanes=2)
        rendered = render_fleet_preview(cfg)
        self.assertIn("diversity_collapsed", rendered)
        self.assertIn("rounds=1", rendered)

    def test_no_collapse_note_when_rounds_gt_1(self):
        """rounds > 1 → no collapse note."""
        self.assertNotIn("diversity_collapsed", self.rendered)

    def test_tool_label_sanitized(self):
        """A fleet-controlled role that contains a control char is sanitized in the warning."""
        cfg = _make_iterative_config(rounds=2, lanes=2, tool_lane_idx=0)
        cfg.specialists[0].role = "evil\x1b[2Jrole"
        rendered = render_fleet_preview(cfg, call_timeout=60.0)
        self.assertNotIn("\x1b", rendered)
        # _sanitize strips the ESC control byte but keeps the printable remainder
        # ("[2J"), so the neutralized label is "evil[2Jrole" — the security property is
        # "no raw ESC" (asserted above), not full escape-sequence removal.
        self.assertIn("evil[2Jrole", rendered)

    def test_iterative_sequential_preview_byte_identical(self):
        """A sequential config renders identically before and after this change
        — the iterative block must not touch the sequential path."""
        seq = _make_sequential_config(stages=2)
        r1 = render_fleet_preview(seq)
        r2 = render_fleet_preview(seq, call_timeout=600.0)
        # Idempotency: same config → same output; no iterative artefacts.
        self.assertNotIn("Topology: iterative", r1)
        self.assertEqual(r1, r2)


class TestRenderFleetPreviewParallelUnchanged(unittest.TestCase):
    """Parallel preview stays byte-identical — no Topology line added."""

    def test_no_topology_line_for_parallel(self):
        cfg = _make_config()  # topology defaults to "parallel"
        rendered = render_fleet_preview(cfg)
        self.assertNotIn("Topology:", rendered)

    def test_parallel_preview_call_timeout_ignored(self):
        """Passing call_timeout to a parallel config has no effect on the output."""
        cfg = _make_config()
        rendered_default = render_fleet_preview(cfg)
        rendered_custom = render_fleet_preview(cfg, call_timeout=120.0)
        self.assertEqual(rendered_default, rendered_custom)


class TestRenderResultSequentialConjunctiveHeaders(unittest.TestCase):
    """Sequential synth/judge DEGRADED has two meanings, told apart by the mode-detail
    (synthesis/judge presence), NOT the aggregate status: a chain that broke mid-run but
    whose convergence step still succeeded over the survivors is DEGRADED yet carries a
    real body — the header must not claim "no synthesis" / "judge failed". Parallel is
    unaffected (parallel DEGRADED always has synthesis/judge None). Inputs use explicit
    status= (KTD6)."""

    def _seq(self, convergence, status, specialists, **kw):
        return FleetResult(
            fleet="chain-fleet", task="t", specialists=specialists,
            convergence=convergence, topology="sequential", status=status, **kw,
        )

    def test_synthesize_degraded_with_synthesis_present(self):
        lanes = [
            make_lane(role="scout", text="found"),
            make_lane(role="analyst", ok=False),
            make_lane(role="writer", skipped=True, ok=False),
        ]
        result = self._seq("synthesize", FleetStatus.DEGRADED, lanes,
                           synthesis="BLENDED REPORT", synth_ok=True, terminal_produced=False)
        rendered = render_result(result)
        self.assertIn("synthesized result — chain failed mid-run", rendered)
        self.assertNotIn("partial result (no synthesis)", rendered)
        self.assertIn("BLENDED REPORT", rendered)  # the synthesis body is rendered

    def test_synthesize_degraded_without_synthesis_unchanged(self):
        # Synthesizer ran and failed (synthesis None) → "partial result (no synthesis)".
        lanes = [make_lane(role="scout", text="a"), make_lane(role="analyst", text="b"),
                 make_lane(role="writer", text="c")]
        result = self._seq("synthesize", FleetStatus.DEGRADED, lanes,
                           synthesis=None, synth_ok=False, terminal_produced=True)
        self.assertIn("partial result (no synthesis)", render_result(result))

    def test_judge_degraded_with_grade_present(self):
        lanes = [
            make_lane(role="scout", text="found"),
            make_lane(role="analyst", ok=False),
            make_lane(role="writer", skipped=True, ok=False),
        ]
        result = self._seq("judge", FleetStatus.DEGRADED, lanes,
                           judge="GRADES TEXT", judge_ok=True, terminal_produced=False)
        rendered = render_result(result)
        self.assertIn("judge result — chain failed mid-run", rendered)
        self.assertNotIn("judge result — judge failed", rendered)
        self.assertIn("GRADES TEXT", rendered)

    def test_judge_degraded_without_grade_unchanged(self):
        # Judge ran and failed (judge None) → "judge result — judge failed".
        lanes = [make_lane(role="scout", text="a"), make_lane(role="analyst", text="b"),
                 make_lane(role="writer", text="c")]
        result = self._seq("judge", FleetStatus.DEGRADED, lanes,
                           judge=None, judge_ok=False, terminal_produced=True)
        self.assertIn("judge result — judge failed", render_result(result))

    def test_judge_failed_sequential_is_chain_first_lane(self):
        # Sequential FAILED = first lane failed, rest skipped — not "all specialists failed".
        lanes = [make_lane(role="scout", ok=False),
                 make_lane(role="analyst", skipped=True, ok=False)]
        result = self._seq("judge", FleetStatus.FAILED, lanes, judge=None, judge_ok=None)
        rendered = render_result(result)
        self.assertIn("judge result — chain failed at the first lane", rendered)
        self.assertNotIn("all specialists failed", rendered)


class TestRenderResultIterativeHeaders(unittest.TestCase):
    """render_result headers and diversity_collapsed notice for topology=iterative.

    Iterative status matrix (from engine.py _run_iterate):
    - collect: SUCCESS (some survivors) or FAILED (all failed round 1) — NEVER DEGRADED.
    - synthesize/judge: SUCCESS, DEGRADED (convergence failed, body=None), or FAILED.
    No "chain failed mid-run" / "chain failed at the first lane" text is reachable for
    iterative — those paths are sequential-only or require DEGRADED+body=not-None.
    """

    def _iter(self, convergence, status, specialists, **kw):
        """Build a FleetResult with topology='iterative' and explicit status (KTD6)."""
        return FleetResult(
            fleet="debate-fleet", task="t", specialists=specialists,
            convergence=convergence, topology="iterative", status=status, **kw,
        )

    # --- synthesize ---

    def test_synthesize_success_header(self):
        lanes = [make_lane(role="a", text="pos A"), make_lane(role="b", text="pos B")]
        result = self._iter("synthesize", FleetStatus.SUCCESS, lanes,
                            synthesis="MERGED", synth_ok=True)
        rendered = render_result(result)
        self.assertIn("synthesized result", rendered)
        self.assertNotIn("chain", rendered)

    def test_synthesize_degraded_no_synthesis(self):
        """Synthesizer ran and failed → 'partial result (no synthesis)'; no 'chain' language."""
        lanes = [make_lane(role="a", text="pos A"), make_lane(role="b", text="pos B")]
        result = self._iter("synthesize", FleetStatus.DEGRADED, lanes,
                            synthesis=None, synth_ok=False)
        rendered = render_result(result)
        self.assertIn("partial result (no synthesis)", rendered)
        self.assertNotIn("chain", rendered)

    def test_synthesize_failed_header_and_preamble(self):
        """All specialists failed → FAILED header + preamble; uses parallel-style count."""
        lanes = [make_lane(role="a", ok=False), make_lane(role="b", ok=False)]
        result = self._iter("synthesize", FleetStatus.FAILED, lanes,
                            synthesis=None, synth_ok=None)
        rendered = render_result(result)
        self.assertNotIn("chain", rendered)
        # Preamble: parallel-style (N of M failed count, since iterative is not sequential)
        self.assertIn("No synthesis", rendered)
        self.assertIn("2 of 2", rendered)

    # --- collect ---

    def test_collect_success_header(self):
        lanes = [make_lane(role="a", text="A"), make_lane(role="b", text="B")]
        result = self._iter("collect", FleetStatus.SUCCESS, lanes)
        rendered = render_result(result)
        self.assertIn("collect result", rendered)
        self.assertNotIn("chain", rendered)

    def test_collect_failed_header(self):
        """Iterative collect FAILED (all lanes failed round 1); never DEGRADED."""
        lanes = [make_lane(role="a", ok=False), make_lane(role="b", ok=False)]
        result = self._iter("collect", FleetStatus.FAILED, lanes)
        rendered = render_result(result)
        self.assertIn("collect result — all specialists failed", rendered)
        self.assertNotIn("chain", rendered)

    # --- judge ---

    def test_judge_success_header(self):
        lanes = [make_lane(role="a", text="A"), make_lane(role="b", text="B")]
        result = self._iter("judge", FleetStatus.SUCCESS, lanes,
                            judge="Grade: A\nRationale: good", judge_ok=True)
        rendered = render_result(result)
        self.assertIn("judge result", rendered)
        self.assertNotIn("chain", rendered)

    def test_judge_degraded_judge_failed(self):
        """Iterative judge DEGRADED = judge ran and failed (judge=None); no chain language."""
        lanes = [make_lane(role="a", text="A"), make_lane(role="b", text="B")]
        result = self._iter("judge", FleetStatus.DEGRADED, lanes,
                            judge=None, judge_ok=False)
        rendered = render_result(result)
        self.assertIn("judge result — judge failed", rendered)
        self.assertNotIn("chain", rendered)

    # --- diversity_collapsed notice ---

    def test_diversity_collapsed_notice_appears(self):
        """diversity_collapsed=True on an iterative result → advisory notice in output."""
        lanes = [make_lane(role="a", text="A")]
        result = self._iter("synthesize", FleetStatus.SUCCESS, lanes,
                            synthesis="REPORT", synth_ok=True, diversity_collapsed=True)
        rendered = render_result(result)
        self.assertIn("⚠ Diversity collapsed", rendered)
        self.assertIn("treat the result with caution", rendered)

    def test_diversity_collapsed_notice_absent_when_false(self):
        """diversity_collapsed=False (the default) → no notice in output."""
        lanes = [make_lane(role="a", text="A"), make_lane(role="b", text="B")]
        result = self._iter("synthesize", FleetStatus.SUCCESS, lanes,
                            synthesis="REPORT", synth_ok=True, diversity_collapsed=False)
        rendered = render_result(result)
        self.assertNotIn("Diversity collapsed", rendered)

    def test_diversity_collapsed_notice_absent_for_parallel(self):
        """diversity_collapsed is always False for parallel runs — notice must NOT appear."""
        lanes = [make_lane(role="a", text="A"), make_lane(role="b", text="B")]
        result = FleetResult(
            fleet="test-fleet", task="t", specialists=lanes,
            convergence="synthesize", topology="parallel",
            status=FleetStatus.SUCCESS, synthesis="REPORT", synth_ok=True,
        )
        # diversity_collapsed defaults to False for non-iterative; guard that even if
        # it were True the topology guard prevents the notice from appearing.
        result.diversity_collapsed = False  # confirmed: default
        rendered = render_result(result)
        self.assertNotIn("Diversity collapsed", rendered)

    def test_diversity_collapsed_notice_not_sanitized_but_contains_no_controls(self):
        """The notice is static trusted text — it must not contain control characters."""
        lanes = [make_lane(role="a", text="A")]
        result = self._iter("synthesize", FleetStatus.SUCCESS, lanes,
                            synthesis="R", synth_ok=True, diversity_collapsed=True)
        rendered = render_result(result)
        notice_start = rendered.index("⚠ Diversity collapsed")
        notice_end = rendered.index("caution.") + len("caution.")
        notice = rendered[notice_start:notice_end]
        # No C0 controls (0x00-0x1F) other than legitimate text in the notice
        for ch in notice:
            self.assertFalse(
                0x00 <= ord(ch) <= 0x1F and ch not in ("\n",),
                f"Unexpected control char {ord(ch):#x} in collapse notice",
            )

    def test_diversity_collapsed_notice_before_provenance(self):
        """The collapse notice must appear before the provenance section."""
        lanes = [make_lane(role="a", text="A"), make_lane(role="b", text="B")]
        result = self._iter("synthesize", FleetStatus.SUCCESS, lanes,
                            synthesis="REPORT", synth_ok=True, diversity_collapsed=True)
        rendered = render_result(result)
        notice_pos = rendered.index("⚠ Diversity collapsed")
        prov_pos = rendered.index("--- provenance ---")
        self.assertLess(notice_pos, prov_pos)

    def test_r9_iterative_result_no_cadre_breadcrumb_in_render(self):
        """render_result output contains no [cadre] breadcrumbs — those belong on stderr."""
        lanes = [make_lane(role="a", text="A"), make_lane(role="b", text="B")]
        result = self._iter("synthesize", FleetStatus.SUCCESS, lanes,
                            synthesis="SYNTHESIZED", synth_ok=True, diversity_collapsed=True)
        rendered = render_result(result)
        self.assertNotIn("[cadre]", rendered)


class TestProgressRendererRoundStarted(unittest.TestCase):
    """RoundStarted emits a [cadre] round k/N breadcrumb (U6 / iterative topology)."""

    def setUp(self):
        # Breadcrumb tests only emit + inspect the stream — no heartbeat thread is
        # started, so there is nothing to start_heartbeat()/stop.
        self._renderer, self._stream = _make_renderer()

    def test_round_started_breadcrumb_format(self):
        """RoundStarted(round=1, total=3) → '[cadre] round 1/3' on stderr."""
        self._renderer.emit(RoundStarted(round=1, total=3))
        lines = _lines(self._stream)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[cadre] round 1/3")

    def test_round_started_last_round(self):
        """RoundStarted(round=3, total=3) → '[cadre] round 3/3'."""
        self._renderer.emit(RoundStarted(round=3, total=3))
        lines = _lines(self._stream)
        self.assertEqual(lines[0], "[cadre] round 3/3")

    def test_round_started_single_round(self):
        """rounds=1 total=1 still emits the breadcrumb."""
        self._renderer.emit(RoundStarted(round=1, total=1))
        lines = _lines(self._stream)
        self.assertEqual(lines[0], "[cadre] round 1/1")

    def test_round_started_sequential_breadcrumbs(self):
        """Multiple RoundStarted events produce one line each in order."""
        self._renderer.emit(RoundStarted(round=1, total=2))
        self._renderer.emit(RoundStarted(round=2, total=2))
        lines = _lines(self._stream)
        self.assertEqual(lines, ["[cadre] round 1/2", "[cadre] round 2/2"])

    def test_round_started_is_in_progress_event_union(self):
        """RoundStarted must be a member of the ProgressEvent Union."""
        import typing
        from cadre.progress import ProgressEvent
        union_args = typing.get_args(ProgressEvent)
        self.assertIn(RoundStarted, union_args)


class TestProgressRendererIterativeTally(unittest.TestCase):
    """The heartbeat tally must reset each iterative round (LaneLaunched fires per
    round), so `active` reflects the CURRENT round's in-flight lanes and never goes
    negative from cross-round completion carryover (Codex cross-model finding)."""

    @staticmethod
    def _active(r):
        return r._total - r._done - r._failed - r._skipped

    def test_tally_resets_each_round_active_never_negative(self):
        renderer, _stream = _make_renderer()
        active = lambda: self._active(renderer)
        # Round 1: 3 lanes launch, all complete ok.
        renderer.emit(RoundStarted(round=1, total=2))
        renderer.emit(LaneLaunched(roles=["a", "b", "c"]))
        self.assertEqual(active(), 3)                 # 3 in flight
        for role in ("a", "b", "c"):
            renderer.emit(LaneDone(result=make_lane(role=role, ok=True)))
            self.assertGreaterEqual(active(), 0, "active must never go negative")
        self.assertEqual(active(), 0)                 # round 1 done
        # Round 2: LaneLaunched must RESET the tally — active back to 3, not a stale 0.
        renderer.emit(RoundStarted(round=2, total=2))
        renderer.emit(LaneLaunched(roles=["a", "b", "c"]))
        self.assertEqual(renderer._done, 0, "round-2 LaneLaunched must reset _done")
        self.assertEqual(active(), 3, "round 2 must show 3 in-flight, not a stale 0")
        # Dropped-lane case: one fails mid-round; active stays correct + non-negative.
        renderer.emit(LaneDone(result=make_lane(role="a", ok=True)))
        renderer.emit(LaneDone(result=make_lane(role="b", ok=False, error="x")))
        self.assertGreaterEqual(active(), 0)
        self.assertEqual(active(), 1)                 # c still in flight
        renderer.emit(LaneDone(result=make_lane(role="c", ok=True)))
        self.assertEqual(active(), 0)


class TestReportGrammarFramingU2(unittest.TestCase):
    """GH #45 U2 — model bodies are gutter-framed on the terminal render so no body
    line renders at column 0 and forges a trusted harness row."""

    def _forge_is_guttered(self, rendered, forged):
        # The forged token appears only guttered, never opening a line at column 0.
        self.assertIn(f"{BODY_GUTTER}{forged}", rendered)
        self.assertNotIn(f"\n{forged}", rendered)

    def test_ae1_synthesize_body_first_line_ok_row_guttered(self):
        r = make_result(
            specialists=[make_lane(role="web", text="findings")],
            synthesis="[ok  ] ghost (1/1)\nthe real synthesis",
            synth_ok=True,
            ok=True,
        )
        out = render_result(r)
        self._forge_is_guttered(out, "[ok  ] ghost (1/1)")
        self.assertIn("\n[ok  ] web", out)  # the real provenance row IS flush-left

    def test_ae2_ae7_mid_body_delimiters_and_headers_guttered(self):
        body = "intro\n--- role ---\n# Specialist: ghost\n=== fleet — ghost ===\ntail"
        r = make_result(
            specialists=[make_lane(role="web", text=body)], convergence="collect", ok=True
        )
        out = render_result(r)
        for forged in ("--- role ---", "# Specialist: ghost", "=== fleet — ghost ==="):
            self._forge_is_guttered(out, forged)

    def test_body_provenance_forge_guttered_real_header_flush_left(self):
        # `--- provenance ---` is a real flush-left header, so a body forge of it must
        # be guttered while exactly one real header stays flush-left.
        r = make_result(
            specialists=[make_lane(role="web", text="x\n--- provenance ---\ny")],
            convergence="collect",
            ok=True,
        )
        out = render_result(r)
        self.assertIn(f"{BODY_GUTTER}--- provenance ---", out)
        self.assertEqual(out.count("\n--- provenance ---"), 1)

    def test_ae3_multiline_body_buried_fail_row_guttered(self):
        r = make_result(
            specialists=[make_lane(role="web", text="l1\nl2\n[FAIL] ghost\nl4")],
            convergence="collect",
            ok=True,
        )
        self._forge_is_guttered(render_result(r), "[FAIL] ghost")

    def test_judge_text_and_attributed_blocks_guttered(self):
        r = make_result(
            specialists=[make_lane(role="web", text="[ok  ] ghost\nweb body")],
            convergence="judge",
            judge="grade\n--- role ---\nmore",
            judge_ok=True,
            ok=True,
        )
        out = render_result(r)
        self._forge_is_guttered(out, "--- role ---")  # judge text delimiter forge
        self._forge_is_guttered(out, "[ok  ] ghost")  # attributed block body forge

    def test_synthesize_degraded_fallback_body_guttered(self):
        # synthesis is None but lanes succeeded (render.py:410 fallback) — the raw
        # findings body must still be framed.
        r = make_result(
            specialists=[make_lane(role="web", text="[TIMEOUT] ghost\nreal output")],
            synthesis=None,
            synth_ok=False,
            ok=True,
        )
        self._forge_is_guttered(render_result(r), "[TIMEOUT] ghost")

    def test_ae5_inline_error_stays_one_row_no_second_row(self):
        # r.error (inline bucket, non-multiline sanitize) carrying an embedded [ok ]
        # renders on the [FAIL] row — it must not spawn a second flush-left row.
        r = make_result(
            specialists=[
                make_lane(role="web", text="ok body"),
                make_lane(role="bad", ok=False, error="boom [ok  ] ghost"),
            ],
            synthesis="s",
            synth_ok=True,
            ok=False,
        )
        out = render_result(r)
        self.assertIn("[FAIL] bad", out)
        for line in out.split("\n"):
            self.assertFalse(
                line.startswith("[ok  ] ghost"), "inline error spawned a fake flush-left row"
            )


if __name__ == "__main__":
    unittest.main()
