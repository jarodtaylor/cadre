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
from fleet_engine.personas import resolve
from fleet_engine.preview_lint import check_focus_grounding

_REPO = Path(__file__).resolve().parents[1]
_RESEARCH_SWARM = _REPO / "fleets" / "research-swarm.example.yaml"
_CODE_REVIEW = _REPO / "fleets" / "code-review.example.yaml"
_DOC_REVIEW = _REPO / "fleets" / "doc-review.example.yaml"
_REVIEW_SCORING = _REPO / "fleets" / "review-scoring.example.yaml"
_RESEARCH_BRIEF = _REPO / "fleets" / "research-brief.example.yaml"
_DEBATE = _REPO / "fleets" / "debate.example.yaml"
_CRITIQUE_REVISE = _REPO / "fleets" / "critique-revise.example.yaml"

# The five review lenses doc-review ports from ce-doc-review, in fleet order.
_DOC_REVIEW_ROLES = ["coherence", "feasibility", "scope-guardian", "product", "adversarial"]

# The four code-review lenses review-scoring uses, in fleet order.
_REVIEW_SCORING_ROLES = ["security", "architecture", "performance", "correctness"]

# The three debate roles and two critique-revise roles, in fleet order.
_DEBATE_ROLES = ["proponent", "skeptic", "contrarian"]
_CRITIQUE_REVISE_ROLES = ["producer", "critic"]


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

    def test_review_scoring_loads(self):
        """review-scoring.example.yaml parses without raising ConfigError."""
        try:
            cfg = FleetConfig.load(_REVIEW_SCORING)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"FleetConfig.load raised on review-scoring: {exc}")
        self.assertIsInstance(cfg, FleetConfig)

    def test_research_brief_loads(self):
        """research-brief.example.yaml parses without raising ConfigError."""
        try:
            cfg = FleetConfig.load(_RESEARCH_BRIEF)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"FleetConfig.load raised on research-brief: {exc}")
        self.assertIsInstance(cfg, FleetConfig)

    def test_debate_loads(self):
        """debate.example.yaml parses without raising ConfigError."""
        try:
            cfg = FleetConfig.load(_DEBATE)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"FleetConfig.load raised on debate: {exc}")
        self.assertIsInstance(cfg, FleetConfig)

    def test_critique_revise_loads(self):
        """critique-revise.example.yaml parses without raising ConfigError."""
        try:
            cfg = FleetConfig.load(_CRITIQUE_REVISE)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"FleetConfig.load raised on critique-revise: {exc}")
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

    def test_review_scoring_is_judge(self):
        """review-scoring uses explicit convergence: judge and has a judge block."""
        cfg = FleetConfig.load(_REVIEW_SCORING)
        self.assertEqual(cfg.convergence, "judge",
                         "review-scoring must parse as judge convergence")
        self.assertIsNotNone(cfg.judge,
                             "review-scoring (judge) must have a judge block")
        self.assertIsNone(cfg.synthesis,
                          "review-scoring (judge) must have synthesis=None")

    def test_research_brief_is_collect_sequential(self):
        """research-brief uses convergence: collect + topology: sequential, with no synthesis."""
        cfg = FleetConfig.load(_RESEARCH_BRIEF)
        self.assertEqual(cfg.convergence, "collect",
                         "research-brief must parse as collect convergence")
        self.assertEqual(cfg.topology, "sequential",
                         "research-brief must parse as sequential topology")
        self.assertIsNone(cfg.synthesis,
                          "research-brief (collect) must have synthesis=None")

    def test_debate_is_collect_iterative(self):
        """debate uses convergence: collect + topology: iterative, rounds: 3, no synthesis."""
        cfg = FleetConfig.load(_DEBATE)
        self.assertEqual(cfg.convergence, "collect",
                         "debate must parse as collect convergence")
        self.assertEqual(cfg.topology, "iterative",
                         "debate must parse as iterative topology")
        self.assertEqual(cfg.rounds, 3,
                         "debate must have rounds == 3")
        self.assertIsNone(cfg.synthesis,
                          "debate (collect) must have synthesis=None")

    def test_critique_revise_is_collect_iterative(self):
        """critique-revise uses convergence: collect + topology: iterative, rounds: 3."""
        cfg = FleetConfig.load(_CRITIQUE_REVISE)
        self.assertEqual(cfg.convergence, "collect",
                         "critique-revise must parse as collect convergence")
        self.assertEqual(cfg.topology, "iterative",
                         "critique-revise must parse as iterative topology")
        self.assertEqual(cfg.rounds, 3,
                         "critique-revise must have rounds == 3")
        self.assertIsNone(cfg.synthesis,
                          "critique-revise (collect) must have synthesis=None")


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
        resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
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
        resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
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

        Every active lane uses toolset: [] (non-retrieval), so the grounding
        lint checks none of them — the warning list is trivially empty. This
        guards against a future maintainer adding a retrieval toolset to a
        review lane without the corresponding sourcing directive.

        Post-U6: doc-review lanes carry personas, so resolution must succeed
        against the real repo pool (not /unused — that would raise).
        """
        cfg = FleetConfig.load(_DOC_REVIEW)
        resolve(cfg, str(_REPO / "personas"))
        warnings = check_focus_grounding(cfg)
        self.assertEqual(
            warnings,
            [],
            f"doc-review should have zero focus-lint warnings, got: {warnings}",
        )

    def test_review_scoring_focus_lint_clean(self):
        """review-scoring has zero check_focus_grounding warnings.

        All four specialist lanes use toolset: [] (non-retrieval), so the
        grounding lint checks none of them — the warning list is trivially
        empty. This guards against a future maintainer adding a retrieval
        toolset to a reviewer lane without the corresponding sourcing directive.
        """
        cfg = FleetConfig.load(_REVIEW_SCORING)
        resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
        warnings = check_focus_grounding(cfg)
        self.assertEqual(
            warnings,
            [],
            f"review-scoring should have zero focus-lint warnings, got: {warnings}",
        )

    def test_research_brief_focus_lint_clean(self):
        """research-brief has zero check_focus_grounding warnings.

        Scout (web, search) and Analyst (web) are retrieval lanes — both focuses
        contain explicit sourcing directives (cite, source, link, primary source),
        so the grounding lint passes for both. Writer (toolset: []) is a
        non-retrieval lane and is skipped by the lint entirely.
        """
        cfg = FleetConfig.load(_RESEARCH_BRIEF)
        resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
        warnings = check_focus_grounding(cfg)
        self.assertEqual(
            warnings,
            [],
            f"research-brief should have zero focus-lint warnings, got: {warnings}",
        )

    def test_debate_focus_lint_clean(self):
        """debate has zero check_focus_grounding warnings.

        All three lanes use toolset: [] (non-retrieval), so the grounding lint
        skips every lane and returns [] trivially. This guards against accidentally
        adding a retrieval toolset to a debate lane without a sourcing directive.
        """
        cfg = FleetConfig.load(_DEBATE)
        resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
        warnings = check_focus_grounding(cfg)
        self.assertEqual(
            warnings,
            [],
            f"debate should have zero focus-lint warnings, got: {warnings}",
        )

    def test_critique_revise_focus_lint_clean(self):
        """critique-revise has zero check_focus_grounding warnings.

        Both lanes use toolset: [] (non-retrieval), so the grounding lint skips
        every lane and returns [] trivially. This guards against accidentally
        adding a retrieval toolset to a lane without a sourcing directive.
        """
        cfg = FleetConfig.load(_CRITIQUE_REVISE)
        resolve(cfg, "/unused")  # focus-only: sets effective_instruction = focus, zero I/O
        warnings = check_focus_grounding(cfg)
        self.assertEqual(
            warnings,
            [],
            f"critique-revise should have zero focus-lint warnings, got: {warnings}",
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


    def test_doc_review_personas_resolve_end_to_end(self):
        """All five doc-review lanes resolve their persona against the repo pool.

        After U6 migration each active lane carries a ``persona`` reference
        (not a ``focus``). This test verifies end-to-end resolution: resolve()
        must not raise, each lane must have a non-empty ``persona`` name, an
        empty ``focus``, ``toolset == []``, and an ``effective_instruction``
        that exactly matches the persona file's text — catching a resolver
        regression that points a lane at the wrong file.
        """
        cfg = FleetConfig.load(_DOC_REVIEW)
        # Must not raise — pool exists and all five persona files are present.
        resolve(cfg, str(_REPO / "personas"))
        for spec in cfg.specialists:
            self.assertTrue(
                spec.persona,
                f"doc-review lane '{spec.role}' must carry a persona name post-U6",
            )
            self.assertEqual(
                spec.focus,
                "",
                f"doc-review lane '{spec.role}' must have empty focus post-U6 "
                f"(persona XOR focus); got: {spec.focus!r}",
            )
            self.assertEqual(
                spec.toolset,
                [],
                f"doc-review lane '{spec.role}' toolset must be [] post-U6",
            )
            # Assert exact contents — not just non-empty — so a resolver regression
            # that returns the wrong persona file fails rather than silently passes.
            expected_text = (_REPO / "personas" / (spec.persona + ".md")).read_text(encoding="utf-8")
            self.assertEqual(
                spec.effective_instruction,
                expected_text,
                f"doc-review lane '{spec.role}' effective_instruction must equal "
                f"the text of personas/{spec.persona}.md",
            )

    def test_doc_review_cross_model_spread_preserved(self):
        """doc-review lanes are not all on the same model (cross-model spread intact).

        The fleet ships with five distinct provider/model families to illustrate
        cross-model review. This guards against accidentally collapsing all lanes
        onto a single model during future edits.
        """
        cfg = FleetConfig.load(_DOC_REVIEW)
        models = [spec.model for spec in cfg.specialists]
        unique_models = set(models)
        self.assertGreater(
            len(unique_models),
            1,
            f"doc-review must use more than one model (cross-model spread); "
            f"got all lanes on: {models}",
        )


# ---------------------------------------------------------------------------
# review-scoring specific invariants: four review lenses, fail-closed toolset,
# a populated judge block, and a criteria-only judge prompt
# ---------------------------------------------------------------------------


class TestReviewScoringFleetInvariants(unittest.TestCase):
    """review-scoring carries four review lanes with fail-closed toolset and a judge block."""

    def test_four_lanes_with_expected_roles(self):
        """review-scoring has exactly four specialist lanes with the expected lens roles."""
        cfg = FleetConfig.load(_REVIEW_SCORING)
        roles = [s.role for s in cfg.specialists]
        self.assertEqual(
            roles,
            _REVIEW_SCORING_ROLES,
            f"review-scoring must have the four lenses in order, got: {roles}",
        )

    def test_every_toolset_is_empty_list_not_none(self):
        """Each parsed specialist toolset is [] (never None) — fail-closed security control.

        Empty toolset is the load-bearing security control — [] is fail-closed
        zero tools (not unset). [] vs None matters at the adapter layer (None
        enables every Hermes toolset; [] is fail-closed zero tools), so assert
        the explicit empty list here.
        """
        cfg = FleetConfig.load(_REVIEW_SCORING)
        for spec in cfg.specialists:
            self.assertEqual(
                spec.toolset,
                [],
                f"review-scoring lane '{spec.role}' toolset must be [] (got {spec.toolset!r})",
            )
            self.assertIsInstance(
                spec.toolset,
                list,
                f"review-scoring lane '{spec.role}' toolset must be a list, not None",
            )

    def test_yaml_authors_explicit_empty_toolset_per_lane(self):
        """Every active specialist lane declares the literal ``toolset: []`` in YAML.

        ``config.py`` normalizes ``toolset: null`` / a missing key to ``[]``, so the
        parsed-config assertion above passes even if a lane were authored with
        ``toolset: null``. That silently weakens the security posture — ``null``
        reads to a human as "default." This test reads the raw YAML and requires
        every non-comment toolset declaration to be exactly ``toolset: []``, and
        that there are exactly four (one per active specialist lane).
        """
        lines = _REVIEW_SCORING.read_text(encoding="utf-8").splitlines()
        toolset_lines = [
            stripped
            for line in lines
            for stripped in [line.strip()]
            if stripped.startswith("toolset:")  # comment lines start with '#', so excluded
        ]
        self.assertEqual(
            len(toolset_lines),
            len(_REVIEW_SCORING_ROLES),
            f"expected one authored toolset per active lane ({len(_REVIEW_SCORING_ROLES)}), "
            f"got {len(toolset_lines)}: {toolset_lines}",
        )
        for decl in toolset_lines:
            self.assertEqual(
                decl,
                "toolset: []",
                f"every active review-scoring lane must author the literal 'toolset: []' "
                f"(fail-closed, not 'toolset: null'); got {decl!r}",
            )

    def test_judge_block_has_provider_and_model(self):
        """The judge block carries a non-empty provider and model (R15, KTD1)."""
        cfg = FleetConfig.load(_REVIEW_SCORING)
        self.assertIsNotNone(cfg.judge, "review-scoring must have a judge block")
        self.assertTrue(cfg.judge.provider, "judge.provider must be non-empty")
        self.assertTrue(cfg.judge.model, "judge.model must be non-empty")

    def test_judge_model_distinct_from_specialist_models(self):
        """The judge uses a different model from all four specialists (cross-model recommendation).

        A same-model judge may over-rate its sibling lane (self-favoring bias).
        The engine does not enforce distinctness (KTD7), so this test guards the
        example fleet's cross-model posture as documented.
        """
        cfg = FleetConfig.load(_REVIEW_SCORING)
        specialist_models = {s.model for s in cfg.specialists}
        self.assertNotIn(
            cfg.judge.model,
            specialist_models,
            f"review-scoring judge model '{cfg.judge.model}' must differ from "
            f"all specialist models {specialist_models} (cross-model recommendation, R16)",
        )

    def test_judge_prompt_is_criteria_only(self):
        """The parsed judge prompt must not contain === LANE: format markers.

        The engine's _judge_prompt() automatically appends the canonical
        per-lane response format (=== LANE: <role> === / Grade: / Rationale:)
        to whatever config.judge.prompt says. If the prompt also contains a
        format block, the model receives two conflicting format instructions
        and the parser may fail. This test reads the PARSED prompt (not raw
        file text) so YAML comments in the fleet file cannot false-positive.
        """
        cfg = FleetConfig.load(_REVIEW_SCORING)
        prompt = cfg.judge.prompt or ""
        self.assertNotIn(
            "=== LANE:",
            prompt,
            "judge prompt must not contain '=== LANE:' — the engine appends the "
            "per-lane format block; two format instructions break the parser",
        )


# ---------------------------------------------------------------------------
# research-brief specific invariants: three sequential lanes, correct toolsets,
# cross-provider spread, and the Analyst's non-collapsibility control
# ---------------------------------------------------------------------------


class TestResearchBriefFleetInvariants(unittest.TestCase):
    """research-brief carries three sequential lanes with the correct toolsets."""

    def test_three_lanes_with_expected_roles(self):
        """research-brief has exactly three lanes: scout, analyst, writer — in order."""
        cfg = FleetConfig.load(_RESEARCH_BRIEF)
        roles = [s.role for s in cfg.specialists]
        self.assertEqual(
            roles,
            ["scout", "analyst", "writer"],
            f"research-brief must have three lanes in order (scout, analyst, writer), got: {roles}",
        )

    def test_scout_toolset_is_web_and_search(self):
        """Scout lane uses toolset: [web, search] (retrieval lane for gathering sources)."""
        cfg = FleetConfig.load(_RESEARCH_BRIEF)
        scout = cfg.specialists[0]
        self.assertEqual(scout.role, "scout")
        self.assertEqual(
            scout.toolset,
            ["web", "search"],
            f"scout toolset must be ['web', 'search'], got: {scout.toolset!r}",
        )

    def test_analyst_toolset_is_web(self):
        """Analyst lane uses toolset: [web] (retrieval lane for live verification)."""
        cfg = FleetConfig.load(_RESEARCH_BRIEF)
        analyst = cfg.specialists[1]
        self.assertEqual(analyst.role, "analyst")
        self.assertEqual(
            analyst.toolset,
            ["web"],
            f"analyst toolset must be ['web'], got: {analyst.toolset!r}",
        )

    def test_writer_toolset_is_empty_list_not_none(self):
        """Writer lane uses toolset: [] — fail-closed zero tools (not None, not unset).

        The writer reasons over prior-stage context only; [] is the deliberate
        fail-closed posture (None enables every Hermes toolset). The [] vs None
        distinction matters at the adapter layer.
        """
        cfg = FleetConfig.load(_RESEARCH_BRIEF)
        writer = cfg.specialists[2]
        self.assertEqual(writer.role, "writer")
        self.assertEqual(
            writer.toolset,
            [],
            f"writer toolset must be [] (fail-closed zero tools), got {writer.toolset!r}",
        )
        self.assertIsInstance(
            writer.toolset,
            list,
            "writer toolset must be a list, not None",
        )

    def test_topology_is_sequential(self):
        """research-brief uses topology: sequential (chain, not parallel fan-out)."""
        cfg = FleetConfig.load(_RESEARCH_BRIEF)
        self.assertEqual(
            cfg.topology,
            "sequential",
            "research-brief topology must be 'sequential'",
        )

    def test_cross_provider_spread(self):
        """Scout and Analyst use different providers (retrieval independence).

        Cross-provider retrieval prevents a shared failure mode from silently
        confirming the Scout's findings in the Analyst's verification pass.
        """
        cfg = FleetConfig.load(_RESEARCH_BRIEF)
        scout_provider = cfg.specialists[0].provider
        analyst_provider = cfg.specialists[1].provider
        self.assertNotEqual(
            scout_provider,
            analyst_provider,
            f"Scout and Analyst must use different providers (cross-provider independence); "
            f"both are on '{scout_provider}'",
        )


# ---------------------------------------------------------------------------
# debate specific invariants: three diverse lanes, iterative topology,
# rounds, fail-closed toolset, and cross-model spread
# ---------------------------------------------------------------------------


class TestDebateFleetInvariants(unittest.TestCase):
    """debate carries exactly three lanes with the expected roles, iterative topology,
    correct rounds count, and fail-closed toolset."""

    def test_three_lanes_with_expected_roles(self):
        """debate has exactly three lanes: proponent, skeptic, contrarian — in order."""
        cfg = FleetConfig.load(_DEBATE)
        roles = [s.role for s in cfg.specialists]
        self.assertEqual(
            roles,
            _DEBATE_ROLES,
            f"debate must have three lanes in order {_DEBATE_ROLES}, got: {roles}",
        )

    def test_topology_is_iterative(self):
        """debate uses topology: iterative."""
        cfg = FleetConfig.load(_DEBATE)
        self.assertEqual(cfg.topology, "iterative",
                         "debate topology must be 'iterative'")

    def test_rounds_is_3(self):
        """debate uses rounds: 3."""
        cfg = FleetConfig.load(_DEBATE)
        self.assertEqual(cfg.rounds, 3, "debate rounds must be 3")

    def test_every_toolset_is_empty_list_not_none(self):
        """Each parsed toolset is [] (never None) — fail-closed security control.

        [] vs None matters at the adapter layer (None enables every Hermes toolset;
        [] is fail-closed zero tools). Assert the explicit empty list for each lane.
        """
        cfg = FleetConfig.load(_DEBATE)
        for spec in cfg.specialists:
            self.assertEqual(
                spec.toolset,
                [],
                f"debate lane '{spec.role}' toolset must be [] (got {spec.toolset!r})",
            )
            self.assertIsInstance(
                spec.toolset,
                list,
                f"debate lane '{spec.role}' toolset must be a list, not None",
            )

    def test_yaml_authors_explicit_empty_toolset_per_lane(self):
        """Every active lane declares the literal ``toolset: []`` in YAML.

        config.py normalizes ``toolset: null`` → [], so the parsed assertion
        above passes even with ``toolset: null``. The explicit ``[]`` is the
        deliberate fail-closed statement; this test reads the raw YAML and
        requires exactly three ``toolset: []`` declarations (one per lane).
        """
        lines = _DEBATE.read_text(encoding="utf-8").splitlines()
        toolset_lines = [
            stripped
            for line in lines
            for stripped in [line.strip()]
            if stripped.startswith("toolset:")
        ]
        self.assertEqual(
            len(toolset_lines),
            len(_DEBATE_ROLES),
            f"expected one authored toolset per active lane ({len(_DEBATE_ROLES)}), "
            f"got {len(toolset_lines)}: {toolset_lines}",
        )
        for decl in toolset_lines:
            self.assertEqual(
                decl,
                "toolset: []",
                f"every active debate lane must author the literal 'toolset: []' "
                f"(fail-closed, not 'toolset: null'); got {decl!r}",
            )

    def test_cross_model_spread(self):
        """debate lanes are not all on the same model (cross-model diversity required).

        Model diversity is the mechanism that drives genuine debate — a same-model
        fleet homogenizes to consensus and defeats the topology's purpose.
        """
        cfg = FleetConfig.load(_DEBATE)
        models = [spec.model for spec in cfg.specialists]
        unique_models = set(models)
        self.assertGreater(
            len(unique_models),
            1,
            f"debate must use more than one model (cross-model diversity); "
            f"got all lanes on: {models}",
        )


# ---------------------------------------------------------------------------
# critique-revise specific invariants: two-lane loop, iterative topology,
# rounds, fail-closed toolset (producer especially), and cross-model spread
# ---------------------------------------------------------------------------


class TestCritiqueReviseFleetInvariants(unittest.TestCase):
    """critique-revise carries exactly two lanes (producer and critic) with the
    correct topology, rounds, and fail-closed toolset."""

    def test_two_lanes_with_expected_roles(self):
        """critique-revise has exactly two lanes: producer, critic — in order."""
        cfg = FleetConfig.load(_CRITIQUE_REVISE)
        roles = [s.role for s in cfg.specialists]
        self.assertEqual(
            roles,
            _CRITIQUE_REVISE_ROLES,
            f"critique-revise must have two lanes in order {_CRITIQUE_REVISE_ROLES}, got: {roles}",
        )

    def test_topology_is_iterative(self):
        """critique-revise uses topology: iterative."""
        cfg = FleetConfig.load(_CRITIQUE_REVISE)
        self.assertEqual(cfg.topology, "iterative",
                         "critique-revise topology must be 'iterative'")

    def test_rounds_is_3(self):
        """critique-revise uses rounds: 3."""
        cfg = FleetConfig.load(_CRITIQUE_REVISE)
        self.assertEqual(cfg.rounds, 3, "critique-revise rounds must be 3")

    def test_producer_toolset_is_empty_list_not_none(self):
        """Producer lane uses toolset: [] — fail-closed zero tools.

        The producer revises from the task and the critic's prior-round feedback
        only; [] bounds the cross-lane injection surface (the critic's text enters
        the producer's context but cannot trigger tool calls). [] vs None matters
        at the adapter layer (None enables every Hermes toolset).
        """
        cfg = FleetConfig.load(_CRITIQUE_REVISE)
        producer = cfg.specialists[0]
        self.assertEqual(producer.role, "producer")
        self.assertEqual(
            producer.toolset,
            [],
            f"producer toolset must be [] (fail-closed zero tools), got {producer.toolset!r}",
        )
        self.assertIsInstance(
            producer.toolset,
            list,
            "producer toolset must be a list, not None",
        )

    def test_every_toolset_is_empty_list_not_none(self):
        """Both parsed toolsets are [] (never None) — fail-closed security control."""
        cfg = FleetConfig.load(_CRITIQUE_REVISE)
        for spec in cfg.specialists:
            self.assertEqual(
                spec.toolset,
                [],
                f"critique-revise lane '{spec.role}' toolset must be [] (got {spec.toolset!r})",
            )
            self.assertIsInstance(
                spec.toolset,
                list,
                f"critique-revise lane '{spec.role}' toolset must be a list, not None",
            )

    def test_yaml_authors_explicit_empty_toolset_per_lane(self):
        """Every active lane declares the literal ``toolset: []`` in YAML.

        config.py normalizes ``toolset: null`` → [], so the parsed assertion
        above passes even with ``toolset: null``. The explicit ``[]`` is the
        deliberate fail-closed statement; this test reads the raw YAML and
        requires exactly two ``toolset: []`` declarations (one per lane).
        """
        lines = _CRITIQUE_REVISE.read_text(encoding="utf-8").splitlines()
        toolset_lines = [
            stripped
            for line in lines
            for stripped in [line.strip()]
            if stripped.startswith("toolset:")
        ]
        self.assertEqual(
            len(toolset_lines),
            len(_CRITIQUE_REVISE_ROLES),
            f"expected one authored toolset per active lane ({len(_CRITIQUE_REVISE_ROLES)}), "
            f"got {len(toolset_lines)}: {toolset_lines}",
        )
        for decl in toolset_lines:
            self.assertEqual(
                decl,
                "toolset: []",
                f"every active critique-revise lane must author the literal 'toolset: []' "
                f"(fail-closed, not 'toolset: null'); got {decl!r}",
            )

    def test_cross_model_spread(self):
        """Producer and critic use different models (cross-model spread).

        Using different models between producer and critic provides a more robust
        revision loop than a same-model pair, which tends toward self-confirmation.
        """
        cfg = FleetConfig.load(_CRITIQUE_REVISE)
        producer_model = cfg.specialists[0].model
        critic_model = cfg.specialists[1].model
        self.assertNotEqual(
            producer_model,
            critic_model,
            f"producer and critic must use different models; both are '{producer_model}'",
        )


class TestUntrustedContentFraming(unittest.TestCase):
    """AE6 (#5, R8): flagship lanes that thread untrusted upstream/sibling/document
    content frame it as untrusted data to audit, never as instructions to follow."""

    def _threading_focuses(self, path, roles):
        cfg = FleetConfig.load(path)
        by_role = {s.role: s for s in cfg.specialists}
        return {r: (by_role[r].focus or "") for r in roles}

    def test_research_brief_threading_lanes_frame_untrusted(self):
        # Analyst audits the Scout's web-derived output; Writer synthesizes prior stages.
        focuses = self._threading_focuses(_RESEARCH_BRIEF, ["analyst", "writer"])
        self.assertIn("untrusted", focuses["analyst"].lower())
        self.assertIn("not instructions", focuses["analyst"].lower())
        self.assertIn("never as", focuses["writer"].lower())
        self.assertIn("instructions", focuses["writer"].lower())

    def test_debate_lanes_frame_prior_rounds_as_untrusted(self):
        focuses = self._threading_focuses(_DEBATE, ["proponent", "skeptic", "contrarian"])
        for role, focus in focuses.items():
            self.assertIn("untrusted data", focus.lower(), f"{role} missing untrusted framing")
            self.assertIn("never as instructions", focus.lower(), f"{role} missing anti-instruction framing")

    def test_critique_revise_lanes_frame_threaded_output_as_untrusted(self):
        focuses = self._threading_focuses(_CRITIQUE_REVISE, ["producer", "critic"])
        for role, focus in focuses.items():
            self.assertIn("untrusted data", focus.lower(), f"{role} missing untrusted framing")
            self.assertIn("never as", focus.lower(), f"{role} missing anti-instruction framing")

    def test_doc_review_personas_frame_document_as_untrusted(self):
        # doc-review lanes read the untrusted --doc document via persona files.
        personas_dir = _REPO / "personas"
        for role in _DOC_REVIEW_ROLES:
            # persona filename convention: <role>-reviewer.md (adversarial is *-document-reviewer)
            candidates = list(personas_dir.glob(f"{role}*reviewer.md"))
            self.assertTrue(candidates, f"no persona file for {role}")
            text = candidates[0].read_text(encoding="utf-8").lower()
            self.assertIn("untrusted", text, f"{role} persona missing untrusted framing")
            self.assertIn("never obey it", text, f"{role} persona missing anti-instruction framing")


if __name__ == "__main__":
    unittest.main()
