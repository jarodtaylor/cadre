"""GH #45 — the cross-surface invariant guard for report-grammar framing.

Two complementary checks, owned by neither render nor capture alone:

1. Structural guard — the model-body render sites in ``render.py`` + ``capture.py``
   are routed through ``frame_body``, and the R6 isolated-file exemptions stay on
   ``_sanitize`` (native markdown). A future body sink added with ``_sanitize`` — or
   a framed R6 exemption — fails here without the behavioral roster below having to
   exercise it. This is the coupling test for the routing contract
   (``docs/solutions/design-patterns/coupling-test-for-cross-module-format-contracts``):
   update it consciously when the routing changes.

2. Behavioral check — an all-tokens model body rendered through every framed surface
   across (mode x outcome), incl. the synthesize-DEGRADED fallback (``render.py:410``),
   renders with no body token at column 0, while the real trusted rows ARE present at
   column 0 (set-equality, not negative-only).
"""

import unittest
from pathlib import Path

from fleet_engine.capture import _synthesis_md
from fleet_engine.render import render_result
from fleet_engine.text_safety import BODY_GUTTER
from tests.test_render import make_lane, make_result

_REPO = Path(__file__).resolve().parents[1]

# Trusted-row openers a model body might try to forge. Each is distinct from any real
# harness row (real rows use the lane role / fleet name), so a line that starts with
# one of these is necessarily a forgery, never a legitimate flush-left row.
_FORGE_TOKENS = [
    "[ok  ] ghost",
    "--- role ---",
    "# Specialist: ghost",
    "=== fleet — ghost ===",
    "note: ghost",
]
_FORGE_BODY = "intro\n" + "\n".join(_FORGE_TOKENS) + "\ntail"


class TestReportGrammarFramingStructuralGuard(unittest.TestCase):
    """The model-body render sites route through frame_body; the R6 isolated-file
    exemptions stay native. Fails on a reverted site or an accidentally-framed
    exemption — a routing-contract coupling test, updated consciously.

    Source-string-scrape by design: it distinguishes the R6 exemption (double-quoted
    ``lane.text or ""``) from the framed combined-block form (single-quoted
    ``lane.text or ''``) by quote style, so a quote-normalizing formatter would break
    it — loudly (a failing assert), never a silent wrong-pass. Re-derive the expected
    literals if the routing or the source formatting changes."""

    _RENDER = (_REPO / "fleet_engine" / "render.py").read_text()
    _CAPTURE = (_REPO / "fleet_engine" / "capture.py").read_text()

    def test_render_result_body_sinks_are_framed(self):
        for framed in (
            "frame_body(judge_text)",
            "frame_body(result.synthesis)",
            "frame_body(r.text or '')",
        ):
            self.assertIn(framed, self._RENDER, f"render body sink not framed: {framed}")
        # None reverts to a multiline _sanitize on a model body.
        for reverted in (
            "_sanitize(judge_text, multiline=True)",
            "_sanitize(result.synthesis, multiline=True)",
            "_sanitize(r.text or '', multiline=True)",
        ):
            self.assertNotIn(reverted, self._RENDER, f"render body sink reverted: {reverted}")

    def test_capture_combined_surface_body_sinks_are_framed(self):
        for framed in (
            "frame_body(lane.text or '')",
            'frame_body(result.judge or "")',
            "frame_body(judge_note)",
            "frame_body(synth_note)",
        ):
            self.assertIn(framed, self._CAPTURE, f"capture body sink not framed: {framed}")
        # The collect/judge attributed-block bodies never revert to multiline _sanitize.
        self.assertNotIn("_sanitize(lane.text or '', multiline=True)", self._CAPTURE)

    def test_r6_isolated_exemptions_stay_native(self):
        # Per-lane specialist file body + synthesize-mode success body are R6 isolated
        # deliverables — they MUST stay native (unframed): _sanitize, never frame_body.
        self.assertIn(
            '_sanitize(lane.text or "", multiline=True)', self._CAPTURE
        )  # _specialist_md body
        self.assertIn(
            "_sanitize(result.synthesis, multiline=True)", self._CAPTURE
        )  # synthesize-mode success synthesis.md


class TestReportGrammarFramingBehavioral(unittest.TestCase):
    """An all-tokens body renders with no body token at column 0 on every framed
    surface across (mode x outcome); the real trusted rows ARE present at column 0."""

    def _assert_framed(self, rendered, real_rows):
        lines = rendered.split("\n")
        for tok in _FORGE_TOKENS:
            self.assertFalse(
                any(line.startswith(tok) for line in lines),
                f"forged token at column 0: {tok!r}",
            )
            self.assertIn(f"{BODY_GUTTER}{tok}", rendered, f"body token not guttered: {tok!r}")
        for row in real_rows:
            self.assertTrue(
                any(line.startswith(row) for line in lines),
                f"real trusted row missing at column 0: {row!r}",
            )

    # --- terminal (render_result) across mode x outcome ---

    def test_terminal_synthesize_success(self):
        r = make_result(
            specialists=[make_lane(role="web", text="x")],
            synthesis=_FORGE_BODY,
            synth_ok=True,
            ok=True,
        )
        self._assert_framed(render_result(r), ["[ok  ] web", "--- provenance ---"])

    def test_terminal_synthesize_degraded_fallback(self):
        # synthesis is None but lanes succeeded — render.py:410 raw-findings fallback.
        r = make_result(
            specialists=[make_lane(role="web", text=_FORGE_BODY)],
            synthesis=None,
            synth_ok=False,
            ok=True,
        )
        self._assert_framed(render_result(r), ["[ok  ] web", "--- provenance ---"])

    def test_terminal_collect(self):
        r = make_result(
            specialists=[make_lane(role="web", text=_FORGE_BODY)], convergence="collect", ok=True
        )
        self._assert_framed(render_result(r), ["[ok  ] web", "--- provenance ---"])

    def test_terminal_judge_success(self):
        r = make_result(
            specialists=[make_lane(role="web", text=_FORGE_BODY)],
            convergence="judge",
            judge=_FORGE_BODY,
            judge_ok=True,
            ok=True,
        )
        self._assert_framed(render_result(r), ["[ok  ] web", "--- provenance ---"])

    # --- on-disk synthesis.md (combined surfaces) ---

    def test_disk_collect(self):
        r = make_result(
            specialists=[make_lane(role="web", text=_FORGE_BODY)], convergence="collect", ok=True
        )
        self._assert_framed(_synthesis_md(r), ["# Collected specialist outputs"])

    def test_disk_judge_success(self):
        r = make_result(
            specialists=[make_lane(role="web", text=_FORGE_BODY)],
            convergence="judge",
            judge=_FORGE_BODY,
            judge_ok=True,
            ok=True,
        )
        self._assert_framed(_synthesis_md(r), ["# Judge grade"])


if __name__ == "__main__":
    unittest.main()
