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
_DOC_REVIEW = _REPO / "fleets" / "doc-review.example.yaml"

# The five review lenses doc-review ports from ce-doc-review, in fleet order.
_DOC_REVIEW_ROLES = ["coherence", "feasibility", "scope-guardian", "product", "adversarial"]

# The exact no-tools declaration every review lane's focus must carry (AE6). It
# is the same anchor code-review uses; asserting the literal phrase keeps the
# security-relevant "you have no tools" instruction from silently regressing.
_NO_TOOLS_DECLARATION = "You have NO tools available"


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

    def test_doc_review_loads(self):
        """doc-review.example.yaml parses without raising ConfigError."""
        try:
            cfg = FleetConfig.load(_DOC_REVIEW)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"FleetConfig.load raised on doc-review: {exc}")
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

    def test_doc_review_is_collect(self):
        """doc-review uses explicit convergence: collect and has no synthesis."""
        cfg = FleetConfig.load(_DOC_REVIEW)
        self.assertEqual(cfg.convergence, "collect",
                         "doc-review must parse as collect convergence")
        self.assertIsNone(cfg.synthesis,
                          "doc-review (collect) must have synthesis=None")


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

    def test_doc_review_focus_lint_clean(self):
        """doc-review has zero check_focus_grounding warnings.

        Every lane uses toolset: [] (non-retrieval), so the grounding lint
        checks none of them — the warning list is trivially empty. This guards
        against a future maintainer adding a retrieval toolset to a review lane
        without the corresponding sourcing directive.
        """
        cfg = FleetConfig.load(_DOC_REVIEW)
        warnings = check_focus_grounding(cfg)
        self.assertEqual(
            warnings,
            [],
            f"doc-review should have zero focus-lint warnings, got: {warnings}",
        )


# ---------------------------------------------------------------------------
# doc-review specific invariants: five lenses, the AE6 no-tools declaration,
# and the KTD4 empty-toolset security guarantee
# ---------------------------------------------------------------------------


class TestDocReviewFleetInvariants(unittest.TestCase):
    """doc-review carries exactly the five review lenses, each grounded + tool-free."""

    def test_five_lanes_with_expected_roles(self):
        """doc-review has exactly five lanes with the ported ce-doc-review lens roles."""
        cfg = FleetConfig.load(_DOC_REVIEW)
        roles = [s.role for s in cfg.specialists]
        self.assertEqual(
            roles,
            _DOC_REVIEW_ROLES,
            f"doc-review must have the five lenses in order, got: {roles}",
        )

    def test_every_focus_declares_no_tools(self):
        """AE6: each lane's focus is non-empty and carries the no-tools declaration.

        ``check_focus_grounding`` skips ``toolset: []`` lanes, so it does NOT
        cover this — every doc-review lane is empty-toolset, so the grounding
        lint never inspects their focus text. This custom assertion is the only
        guard that each review lens actually tells its model it has no tools.
        """
        cfg = FleetConfig.load(_DOC_REVIEW)
        for spec in cfg.specialists:
            self.assertTrue(
                spec.focus.strip(),
                f"doc-review lane '{spec.role}' must have a non-empty focus",
            )
            self.assertIn(
                _NO_TOOLS_DECLARATION,
                spec.focus,
                f"doc-review lane '{spec.role}' focus must declare it has no tools "
                f"(missing {_NO_TOOLS_DECLARATION!r})",
            )

    def test_every_toolset_is_empty_list_not_none(self):
        """KTD4: each parsed toolset is [] (an empty list), never None.

        Empty toolset is the load-bearing security control — the sole mitigation
        keeping prompt-injection in the (untrusted) reviewed document from
        escalating to actions. The [] vs None distinction matters at the adapter
        layer (None enables every Hermes toolset; [] is fail-closed zero tools),
        so assert the explicit empty list here. Note config.py normalizes
        ``toolset: null`` -> [], so this passes even on a null authoring slip;
        the explicit ``toolset: []`` in the YAML is the authored guarantee, which
        ``test_yaml_authors_explicit_empty_toolset_per_lane`` guards directly.
        """
        cfg = FleetConfig.load(_DOC_REVIEW)
        for spec in cfg.specialists:
            self.assertEqual(
                spec.toolset,
                [],
                f"doc-review lane '{spec.role}' toolset must be [] (got {spec.toolset!r})",
            )
            self.assertIsInstance(
                spec.toolset,
                list,
                f"doc-review lane '{spec.role}' toolset must be a list, not None",
            )

    def test_yaml_authors_explicit_empty_toolset_per_lane(self):
        """KTD4 (authored guarantee): every active lane declares the literal ``toolset: []``.

        ``config.py`` normalizes ``toolset: null`` / a missing key both to ``[]``,
        so the parsed-config assertion above passes even if a lane were authored
        with ``toolset: null``. That would silently weaken the security posture:
        ``null`` reads to a human as "default" and is one careless edit away from
        being dropped, whereas the explicit ``[]`` is the deliberate fail-closed
        statement. This test reads the raw YAML and requires every non-comment
        toolset declaration to be exactly ``toolset: []`` — and that there are
        exactly five (one per active lane; the commented-out optional design/
        security lanes are excluded as comments).
        """
        lines = _DOC_REVIEW.read_text(encoding="utf-8").splitlines()
        toolset_lines = [
            stripped
            for line in lines
            for stripped in [line.strip()]
            if stripped.startswith("toolset:")  # comment lines start with '#', so excluded
        ]
        self.assertEqual(
            len(toolset_lines),
            len(_DOC_REVIEW_ROLES),
            f"expected one authored toolset per active lane ({len(_DOC_REVIEW_ROLES)}), "
            f"got {len(toolset_lines)}: {toolset_lines}",
        )
        for decl in toolset_lines:
            self.assertEqual(
                decl,
                "toolset: []",
                f"every active doc-review lane must author the literal 'toolset: []' "
                f"(fail-closed, not 'toolset: null'); got {decl!r}",
            )


if __name__ == "__main__":
    unittest.main()
