"""Tests for fleet_engine/judge_grade.py — the judge-grade parser.

Covers:
- Happy path: all lanes graded, grade forms, multi-line rationale
- Partial coverage: some lanes ungraded
- Label drift (KTD9: fail toward false-partial)
- Non-survivor labels silently ignored
- Degrade paths: empty/garbled text, missing grade field
- Coupling test: _judge_prompt ↔ parse_grades format contract (critical)
"""

import unittest

from fleet_engine.judge_grade import ParsedGrades, parse_grades


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lanes(*pairs):
    """Return a list of (role, model) tuples."""
    return list(pairs)


# ---------------------------------------------------------------------------
# Happy path: all lanes graded
# ---------------------------------------------------------------------------


class TestWellFormedAllLanes(unittest.TestCase):
    """Well-formed judge text for ALL surviving lanes → one entry per lane."""

    def setUp(self):
        self.lanes = [("web", "web/model"), ("social", "grok"), ("analysis", "ana/model")]
        self.response = (
            "=== LANE: web ===\n"
            "Grade: 9/10\n"
            "Rationale: Strong web research with sourced claims.\n"
            "\n"
            "=== LANE: social ===\n"
            "Grade: PASS\n"
            "Rationale: Good social coverage.\n"
            "\n"
            "=== LANE: analysis ===\n"
            "Grade: B+\n"
            "Rationale: Solid analysis.\n"
        )
        self.result = parse_grades(self.response, self.lanes)

    def test_parsed_ok_true(self):
        self.assertTrue(self.result.parsed_ok)

    def test_one_entry_per_lane(self):
        self.assertEqual(len(self.result.entries), 3)

    def test_ungraded_empty(self):
        self.assertEqual(self.result.ungraded, [])

    def test_all_roles_present(self):
        roles = {e["role"] for e in self.result.entries}
        self.assertEqual(roles, {"web", "social", "analysis"})

    def test_model_comes_from_lane_tuple(self):
        """Model in each entry comes from the matched lane, not the judge text."""
        by_role = {e["role"]: e for e in self.result.entries}
        self.assertEqual(by_role["web"]["model"], "web/model")
        self.assertEqual(by_role["social"]["model"], "grok")
        self.assertEqual(by_role["analysis"]["model"], "ana/model")

    def test_model_from_lane_even_when_judge_prose_differs(self):
        """Entry model is taken from the surviving lane tuple, never judge text.

        Even if the judge's prose mentions a model name (or none at all),
        the entry must carry the lane's model string verbatim.
        """
        lanes = [("web", "real-model")]
        response = (
            "=== LANE: web ===\n"
            "Grade: A\n"
            "Rationale: web did well (evaluated with wrong-model in my text).\n"
        )
        result = parse_grades(response, lanes)
        self.assertTrue(result.parsed_ok)
        self.assertEqual(result.entries[0]["model"], "real-model")

    def test_each_entry_has_required_keys(self):
        for entry in self.result.entries:
            self.assertIn("role", entry)
            self.assertIn("model", entry)
            self.assertIn("grade", entry)
            self.assertIn("rationale", entry)


# ---------------------------------------------------------------------------
# Grade values preserved verbatim (R7)
# ---------------------------------------------------------------------------


class TestGradeValuesPreservedVerbatim(unittest.TestCase):
    """Grade values of different forms all survive as strings (R7)."""

    def _grade_for(self, grade_str):
        response = f"=== LANE: r ===\nGrade: {grade_str}\nRationale: test\n"
        result = parse_grades(response, [("r", "m")])
        self.assertTrue(result.parsed_ok)
        return result.entries[0]["grade"]

    def test_numeric_fraction_preserved(self):
        self.assertEqual(self._grade_for("7/10"), "7/10")

    def test_pass_preserved(self):
        self.assertEqual(self._grade_for("PASS"), "PASS")

    def test_letter_grade_preserved(self):
        self.assertEqual(self._grade_for("B+"), "B+")

    def test_fail_preserved(self):
        self.assertEqual(self._grade_for("FAIL"), "FAIL")

    def test_decimal_preserved(self):
        self.assertEqual(self._grade_for("8.5"), "8.5")


# ---------------------------------------------------------------------------
# Multi-line rationale
# ---------------------------------------------------------------------------


class TestMultiLineRationale(unittest.TestCase):

    def test_rationale_spanning_multiple_lines_captured_in_full(self):
        response = (
            "=== LANE: reviewer ===\n"
            "Grade: B\n"
            "Rationale: First line of rationale.\n"
            "Second line continues here.\n"
            "And a third line.\n"
        )
        result = parse_grades(response, [("reviewer", "m/m")])
        self.assertTrue(result.parsed_ok)
        rationale = result.entries[0]["rationale"]
        self.assertIn("First line", rationale)
        self.assertIn("Second line", rationale)
        self.assertIn("third line", rationale)

    def test_rationale_stops_at_next_lane_marker(self):
        """Lane1's rationale must not bleed into lane2's block."""
        response = (
            "=== LANE: lane1 ===\n"
            "Grade: A\n"
            "Rationale: Rationale for lane1.\n"
            "It continues here.\n"
            "\n"
            "=== LANE: lane2 ===\n"
            "Grade: B\n"
            "Rationale: Rationale for lane2 only.\n"
        )
        result = parse_grades(response, [("lane1", "m1"), ("lane2", "m2")])
        self.assertTrue(result.parsed_ok)
        by_role = {e["role"]: e for e in result.entries}
        # lane1's rationale must NOT contain lane2's rationale text
        self.assertNotIn("lane2 only", by_role["lane1"]["rationale"])
        # lane2's rationale must be present and correct
        self.assertIn("lane2 only", by_role["lane2"]["rationale"])


# ---------------------------------------------------------------------------
# Partial coverage
# ---------------------------------------------------------------------------


class TestPartialCoverage(unittest.TestCase):
    """Judge text grades 3 of 5 surviving lanes → partial result, parsed_ok still True."""

    def setUp(self):
        self.lanes = [
            ("reviewer1", "m1"), ("reviewer2", "m2"), ("reviewer3", "m3"),
            ("reviewer4", "m4"), ("reviewer5", "m5"),
        ]
        # Judge graded only 3 of the 5
        self.response = (
            "=== LANE: reviewer1 ===\n"
            "Grade: 8/10\n"
            "Rationale: Good findings.\n"
            "\n"
            "=== LANE: reviewer3 ===\n"
            "Grade: PASS\n"
            "Rationale: Clear and well-sourced.\n"
            "\n"
            "=== LANE: reviewer5 ===\n"
            "Grade: B\n"
            "Rationale: Adequate coverage.\n"
        )
        self.result = parse_grades(self.response, self.lanes)

    def test_parsed_ok_true_for_partial_coverage(self):
        self.assertTrue(self.result.parsed_ok)

    def test_three_entries_produced(self):
        self.assertEqual(len(self.result.entries), 3)

    def test_two_lanes_are_ungraded(self):
        self.assertEqual(len(self.result.ungraded), 2)
        ungraded_roles = {r for (r, _) in self.result.ungraded}
        self.assertEqual(ungraded_roles, {"reviewer2", "reviewer4"})

    def test_ungraded_carries_model_from_surviving_lane(self):
        """ungraded entries carry the lane model, not a placeholder."""
        ungraded_by_role = {r: m for (r, m) in self.result.ungraded}
        self.assertEqual(ungraded_by_role["reviewer2"], "m2")
        self.assertEqual(ungraded_by_role["reviewer4"], "m4")


# ---------------------------------------------------------------------------
# Label drift → fail toward false-partial (KTD9)
# ---------------------------------------------------------------------------


class TestLabelDrift(unittest.TestCase):
    """A drifted or paraphrased label misses the match → lane is ungraded (not false-full)."""

    def test_paraphrased_label_lands_in_ungraded(self):
        lanes = [("Security Reviewer", "m1"), ("Logic Checker", "m2")]
        response = (
            "=== LANE: Security Reviewer ===\n"
            "Grade: PASS\n"
            "Rationale: No issues found.\n"
            "\n"
            "=== LANE: Reviewer 2 ===\n"  # paraphrased — NOT "Logic Checker"
            "Grade: B\n"
            "Rationale: Minor issues.\n"
        )
        result = parse_grades(response, lanes)
        self.assertTrue(result.parsed_ok)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0]["role"], "Security Reviewer")
        # "Logic Checker" got no usable match → ungraded
        ungraded_roles = {r for (r, _) in result.ungraded}
        self.assertIn("Logic Checker", ungraded_roles)

    def test_case_sensitive_match_fails_on_wrong_case(self):
        """Role matching is CASE-SENSITIVE: 'web' ≠ 'Web'.

        The LANE marker keyword is matched case-insensitively (=== vs ===),
        but the LABEL must match the surviving lane role exactly (case-sensitive).
        A wrong-case label fails toward false-partial.
        """
        lanes = [("web", "m")]
        response = "=== LANE: Web ===\nGrade: A\nRationale: test.\n"
        result = parse_grades(response, lanes)
        # 'Web' ≠ 'web': no entry produced; 'web' must land in ungraded
        self.assertFalse(result.parsed_ok)
        self.assertEqual(result.entries, [])
        ungraded_roles = {r for (r, _) in result.ungraded}
        self.assertIn("web", ungraded_roles)

    def test_lane_marker_keyword_is_case_insensitive(self):
        """The '=== LANE:' keyword itself is matched case-insensitively."""
        lanes = [("role1", "m")]
        response = "=== lane: role1 ===\nGrade: A\nRationale: test.\n"
        result = parse_grades(response, lanes)
        self.assertTrue(result.parsed_ok)
        self.assertEqual(result.entries[0]["role"], "role1")


# ---------------------------------------------------------------------------
# Non-survivor labels are ignored
# ---------------------------------------------------------------------------


class TestNonSurvivorLabel(unittest.TestCase):
    """A block whose label matches no surviving lane is ignored — never invented."""

    def test_extra_label_in_judge_text_is_not_added_as_entry(self):
        lanes = [("real-lane", "m")]
        response = (
            "=== LANE: real-lane ===\n"
            "Grade: A\n"
            "Rationale: Good.\n"
            "\n"
            "=== LANE: invented-lane ===\n"
            "Grade: B\n"
            "Rationale: This lane was never in surviving_lanes.\n"
        )
        result = parse_grades(response, lanes)
        self.assertTrue(result.parsed_ok)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0]["role"], "real-lane")
        roles = {e["role"] for e in result.entries}
        self.assertNotIn("invented-lane", roles)

    def test_invented_label_does_not_appear_in_ungraded(self):
        """An invented label that matches no surviving lane is not even in ungraded."""
        lanes = [("real", "m")]
        response = (
            "=== LANE: real ===\n"
            "Grade: A\n"
            "Rationale: Good.\n"
            "\n"
            "=== LANE: fake ===\n"
            "Grade: C\n"
            "Rationale: Fabricated.\n"
        )
        result = parse_grades(response, lanes)
        ungraded_roles = {r for (r, _) in result.ungraded}
        self.assertNotIn("fake", ungraded_roles)


# ---------------------------------------------------------------------------
# Degrade paths
# ---------------------------------------------------------------------------


class TestDegradePaths(unittest.TestCase):
    """Unparseable or empty input degrades cleanly — no crash, parsed_ok=False."""

    def test_empty_string_returns_all_lanes_ungraded(self):
        result = parse_grades("", [("role", "m")])
        self.assertFalse(result.parsed_ok)
        self.assertEqual(result.entries, [])
        ungraded_roles = {r for (r, _) in result.ungraded}
        self.assertIn("role", ungraded_roles)

    def test_whitespace_only_string(self):
        result = parse_grades("   \n\t  ", [("role", "m")])
        self.assertFalse(result.parsed_ok)
        self.assertEqual(result.entries, [])

    def test_text_with_no_lane_markers(self):
        result = parse_grades("The judge found everything to be fine.", [("role", "m")])
        self.assertFalse(result.parsed_ok)
        self.assertEqual(result.entries, [])

    def test_lane_marker_present_but_no_grade_field(self):
        """A block matching a survivor but carrying no Grade: field → lane is ungraded."""
        response = "=== LANE: role ===\nRationale: Something here.\n"
        result = parse_grades(response, [("role", "m")])
        self.assertFalse(result.parsed_ok)
        self.assertEqual(result.entries, [])
        ungraded_roles = {r for (r, _) in result.ungraded}
        self.assertIn("role", ungraded_roles)

    def test_empty_surviving_lanes_yields_no_entries_no_ungraded(self):
        """No surviving lanes → empty result; the judge block is simply ignored."""
        response = "=== LANE: web ===\nGrade: A\nRationale: Good.\n"
        result = parse_grades(response, [])
        self.assertFalse(result.parsed_ok)
        self.assertEqual(result.entries, [])
        self.assertEqual(result.ungraded, [])

    def test_garbled_text_does_not_raise(self):
        """Arbitrary garbled input → valid ParsedGrades, no exception."""
        garbled = "\x00\x01\x02 random bytes and ===LANE: stuff with no structure"
        result = parse_grades(garbled, [("r", "m")])
        self.assertIsInstance(result, ParsedGrades)

    def test_returns_parsed_grades_instance(self):
        """Return type is always ParsedGrades, even on total parse failure."""
        result = parse_grades("", [])
        self.assertIsInstance(result, ParsedGrades)


# ---------------------------------------------------------------------------
# Purity guard: judge_grade must never import fleet_engine.engine
# ---------------------------------------------------------------------------


class TestJudgeGradePurity(unittest.TestCase):
    """judge_grade.py is a caller-layer module: it must NOT import fleet_engine.engine."""

    def test_module_does_not_import_engine(self):
        import inspect
        import fleet_engine.judge_grade as mod

        source = inspect.getsource(mod)
        # Check only actual import statements (not docstrings that say "never imports X").
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        engine_imports = [l for l in import_lines if "fleet_engine.engine" in l]
        self.assertEqual(
            engine_imports,
            [],
            "judge_grade.py must never import fleet_engine.engine — "
            f"found: {engine_imports}",
        )


# ---------------------------------------------------------------------------
# Coupling test: engine's _judge_prompt ↔ parse_grades (critical)
# ---------------------------------------------------------------------------


class TestCouplingWithEngine(unittest.TestCase):
    """Critical: _judge_prompt's format matches parse_grades' token expectations.

    This class is the living contract between U2 (engine) and U3 (parser).
    If either side drifts — new marker syntax, renamed keyword, altered casing —
    these tests fail before it reaches a live run.

    The test file is explicitly ALLOWED to import fleet_engine.engine
    (the coupling test must reach the real prompt builder); judge_grade.py
    itself must not (see TestJudgeGradePurity).
    """

    def _judge_cfg(self):
        from fleet_engine.config import FleetConfig
        from fleet_engine.personas import resolve

        data = {
            "name": "t",
            "convergence": "judge",
            "judge": {"provider": "openrouter", "model": "judge/model"},
            "specialists": [
                {
                    "role": "web",
                    "provider": "openrouter",
                    "model": "web/model",
                    "toolset": ["web"],
                    "focus": "web research",
                },
                {
                    "role": "social",
                    "provider": "xai",
                    "model": "grok",
                    "toolset": ["x_search"],
                    "focus": "social scan",
                },
            ],
        }
        cfg = FleetConfig.from_dict(data)
        resolve(cfg, "/unused")
        return cfg

    def _make_successes(self):
        from fleet_engine.model_client import AgentResult

        return [
            AgentResult(
                role="web", provider="openrouter", model="web/model",
                ok=True, text="web findings here",
            ),
            AgentResult(
                role="social", provider="xai", model="grok",
                ok=True, text="social findings here",
            ),
        ]

    def test_judge_prompt_contains_lane_marker_token(self):
        """_judge_prompt emits '=== LANE:' — the primary token parse_grades keys on."""
        from fleet_engine.engine import _judge_prompt

        prompt = _judge_prompt(self._judge_cfg(), "test task", self._make_successes())
        self.assertIn(
            "=== LANE:",
            prompt,
            "_judge_prompt must include the '=== LANE:' marker the parser keys on",
        )

    def test_judge_prompt_contains_grade_token(self):
        """_judge_prompt emits 'Grade:' — the field parse_grades extracts the grade from."""
        from fleet_engine.engine import _judge_prompt

        prompt = _judge_prompt(self._judge_cfg(), "test task", self._make_successes())
        self.assertIn("Grade:", prompt, "_judge_prompt must include the 'Grade:' label")

    def test_judge_prompt_contains_rationale_token(self):
        """_judge_prompt emits 'Rationale:' — the field parse_grades extracts rationale from."""
        from fleet_engine.engine import _judge_prompt

        prompt = _judge_prompt(self._judge_cfg(), "test task", self._make_successes())
        self.assertIn("Rationale:", prompt, "_judge_prompt must include the 'Rationale:' label")

    def test_canonical_format_response_round_trips_correctly(self):
        """A response in the exact format _judge_prompt instructs for parses to correct entries.

        This is the end-to-end format contract: build the prompt, hand-craft a
        response that follows it to the letter, feed through parse_grades, and
        assert every invariant (correct entries, ungraded empty, models from lanes).
        """
        successes = self._make_successes()
        surviving_lanes = [(r.role, r.model) for r in successes]

        # A canonical response using the exact format the engine prompts for
        response = (
            "=== LANE: web ===\n"
            "Grade: PASS\n"
            "Rationale: Excellent web research with strong citations.\n"
            "\n"
            "=== LANE: social ===\n"
            "Grade: B+\n"
            "Rationale: Good social coverage, minor gaps.\n"
        )
        result = parse_grades(response, surviving_lanes)

        self.assertTrue(result.parsed_ok)
        self.assertEqual(len(result.entries), 2)
        self.assertEqual(result.ungraded, [])

        by_role = {e["role"]: e for e in result.entries}
        self.assertIn("web", by_role)
        self.assertIn("social", by_role)
        # Model comes from the lane tuple, never from judge text
        self.assertEqual(
            by_role["web"]["model"], "web/model",
            "web model must come from the surviving lane tuple",
        )
        self.assertEqual(
            by_role["social"]["model"], "grok",
            "social model must come from the surviving lane tuple",
        )
        # Grade forms preserved verbatim
        self.assertEqual(by_role["web"]["grade"], "PASS")
        self.assertEqual(by_role["social"]["grade"], "B+")


if __name__ == "__main__":
    unittest.main()
