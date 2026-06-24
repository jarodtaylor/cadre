"""Positive-example guarantee tests for the U7 starter fleet templates.

Both ``research-swarm.example.yaml`` and ``code-review.example.yaml`` must:
- Load cleanly via ``FleetConfig.load`` (no ConfigError).
- Parse with the expected convergence mode and synthesis presence.
- Return zero ``check_focus_grounding`` warnings (the lint-clean guarantee).

We do NOT assert ``check_palette`` is clean: the reference provider/model values
are example assignments the operator swaps from their own palette, so palette
warnings are expected on any dev host without a matching ``~/.cadre/palette.yaml``.
Only the focus-grounding lint must be zero.

Paths are resolved relative to the repo root (``tests/../fleets/``), following
the ``_EXAMPLE_FLEET`` pattern in ``test_render.py``.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from fleet_engine.config import FleetConfig
from fleet_engine.preview_lint import check_focus_grounding

_REPO = Path(__file__).resolve().parents[1]
_RESEARCH_SWARM = _REPO / "fleets" / "research-swarm.example.yaml"
_CODE_REVIEW = _REPO / "fleets" / "code-review.example.yaml"


# ---------------------------------------------------------------------------
# Schema validity: both fleets load without raising ConfigError
# ---------------------------------------------------------------------------


class TestStarterFleetsLoad(unittest.TestCase):
    """Both starter fleet templates load as valid FleetConfig objects."""

    def test_research_swarm_loads(self):
        """research-swarm.example.yaml parses without raising ConfigError."""
        try:
            cfg = FleetConfig.load(_RESEARCH_SWARM)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"FleetConfig.load raised on research-swarm: {exc}")
        self.assertIsInstance(cfg, FleetConfig)

    def test_code_review_loads(self):
        """code-review.example.yaml parses without raising ConfigError."""
        try:
            cfg = FleetConfig.load(_CODE_REVIEW)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"FleetConfig.load raised on code-review: {exc}")
        self.assertIsInstance(cfg, FleetConfig)


# ---------------------------------------------------------------------------
# Convergence mode: each fleet parses as the expected shape
# ---------------------------------------------------------------------------


class TestStarterFleetsConvergence(unittest.TestCase):
    """Each starter fleet carries the expected convergence mode."""

    def test_research_swarm_is_synthesize(self):
        """research-swarm omits convergence (defaults to 'synthesize') and has synthesis."""
        cfg = FleetConfig.load(_RESEARCH_SWARM)
        self.assertEqual(cfg.convergence, "synthesize",
                         "research-swarm must default to synthesize convergence")
        self.assertIsNotNone(cfg.synthesis,
                             "research-swarm must have a synthesis block")

    def test_code_review_is_collect(self):
        """code-review uses explicit convergence: collect and has no synthesis."""
        cfg = FleetConfig.load(_CODE_REVIEW)
        self.assertEqual(cfg.convergence, "collect",
                         "code-review must parse as collect convergence")
        self.assertIsNone(cfg.synthesis,
                          "code-review (collect) must have synthesis=None")


# ---------------------------------------------------------------------------
# Focus-grounding lint: both fleets return zero warnings — THE guarantee
# ---------------------------------------------------------------------------


class TestStarterFleetsLintClean(unittest.TestCase):
    """check_focus_grounding returns [] for both starter fleets.

    This is the positive-example guarantee: a user copying either fleet as a
    starting point sees zero lint warnings on --preview, confirming that the
    templates demonstrate correct grounding discipline.

    Note: ``check_palette`` is intentionally NOT asserted here — the example
    provider/model values are placeholders the operator swaps from their
    palette, so palette warnings are expected and fine.
    """

    def test_research_swarm_focus_lint_clean(self):
        """research-swarm has zero check_focus_grounding warnings."""
        cfg = FleetConfig.load(_RESEARCH_SWARM)
        warnings = check_focus_grounding(cfg)
        self.assertEqual(
            warnings,
            [],
            f"research-swarm should have zero focus-lint warnings, got: {warnings}",
        )

    def test_code_review_focus_lint_clean(self):
        """code-review has zero check_focus_grounding warnings.

        All four lanes use toolset: [] (non-retrieval), so none are checked.
        This test guards against accidentally adding a retrieval toolset to a
        reviewer lane without the corresponding sourcing directive.
        """
        cfg = FleetConfig.load(_CODE_REVIEW)
        warnings = check_focus_grounding(cfg)
        self.assertEqual(
            warnings,
            [],
            f"code-review should have zero focus-lint warnings, got: {warnings}",
        )

    def test_code_review_all_lanes_non_retrieval(self):
        """All code-review lanes use toolset: [] so none are retrieval-checked.

        This is the structural reason code-review is lint-exempt: the focus
        grounding check only applies to retrieval-toolset lanes. A non-empty
        retrieval toolset on any reviewer lane would require a sourcing term.
        """
        from fleet_engine.preview_lint import RETRIEVAL_TOOLSETS

        cfg = FleetConfig.load(_CODE_REVIEW)
        for spec in cfg.specialists:
            retrieval_intersection = set(spec.toolset) & RETRIEVAL_TOOLSETS
            self.assertEqual(
                retrieval_intersection,
                set(),
                f"code-review lane '{spec.role}' must not have retrieval toolset; "
                f"got toolset={spec.toolset}, retrieval={retrieval_intersection}",
            )


if __name__ == "__main__":
    unittest.main()
