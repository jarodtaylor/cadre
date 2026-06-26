"""Direct unit tests for fleet_engine/render.py render_result and render_fleet_preview.

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

from fleet_engine.config import FleetConfig, SpecialistSpec, SynthesisSpec
from fleet_engine.engine import FleetResult
from fleet_engine.model_client import AgentResult
from fleet_engine.personas import resolve
from fleet_engine.progress import (
    Completion,
    LaneDone,
    LaneLaunched,
    RunFolder,
    SynthDone,
    SynthStarted,
    Validated,
)
from fleet_engine.render import (
    ProgressRenderer,
    render_file_inputs,
    render_fleet_preview,
    render_result,
)

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
    convergence="synthesize",
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
        convergence=convergence,
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
        from fleet_engine.config import SpecialistSpec
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
        from fleet_engine.config import SpecialistSpec
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

    def test_model_output_not_sanitized(self):
        """r.text and result.synthesis are NOT stripped (model output, deferred chain)."""
        lane = make_lane(role="web", ok=True, text="output with \x1b[31m escape")
        result = make_result(specialists=[lane], synthesis="synth with \x1b escape", synth_ok=True, ok=True)
        rendered = render_result(result)
        # The ESC in text and synthesis must still be present (not stripped by render_result).
        self.assertIn("\x1b", rendered)


# ---------------------------------------------------------------------------
# New tests: _sanitize strips Unicode line separators and bidi controls (FIX 1)
# ---------------------------------------------------------------------------


class TestSanitizeUnicodeExclusions(unittest.TestCase):
    """_sanitize drops Unicode line/paragraph separators and bidi format controls."""

    def setUp(self):
        from fleet_engine.render import _sanitize
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
    """The synthesis text is model output and is NOT sanitized; this test verifies
    that a multiline synthesis prompt with a tab in the fleet preview (which IS
    sanitized with multiline=True) preserves the tab."""

    def setUp(self):
        from fleet_engine.render import _sanitize
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
        # Shape: [cadre] heartbeat mm:ss active=A/T done=D failed=F
        pattern = r"^\[cadre\] heartbeat \d{2}:\d{2} active=\d+/\d+ done=\d+ failed=\d+$"
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
# render_file_inputs — the --doc resolved-path preview block (U2)
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


if __name__ == "__main__":
    unittest.main()
