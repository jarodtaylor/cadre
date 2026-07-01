import os
import tempfile
import unittest

from fleet_engine.config import MAX_ROUNDS, ConfigError, FleetConfig, JudgeSpec, SpecialistSpec, SynthesisSpec


def make_data(**overrides):
    """Canonical valid fleet dict; override one block per test (Tonbi-style fixture)."""
    data = {
        "name": "test-swarm",
        "synthesis": {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "prompt": "synthesize",
        },
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "google/gemini-3-flash",
             "toolset": ["web"], "focus": "find sources"},
            {"role": "social", "provider": "xai", "model": "grok-4.3",
             "toolset": ["x_search"], "focus": "scan X"},
        ],
    }
    data.update(overrides)
    return data


class TestValidConfig(unittest.TestCase):
    def test_loads_into_typed_objects(self):
        cfg = FleetConfig.from_dict(make_data())
        self.assertEqual(cfg.name, "test-swarm")
        self.assertIsInstance(cfg.synthesis, SynthesisSpec)
        self.assertEqual(cfg.synthesis.provider, "openrouter")
        self.assertEqual(len(cfg.specialists), 2)
        self.assertEqual(cfg.specialists[0].role, "web")
        self.assertEqual(cfg.specialists[1].provider, "xai")
        self.assertEqual(cfg.specialists[1].model, "grok-4.3")

    def test_read_and_search_toolsets_need_no_optin(self):
        cfg = FleetConfig.from_dict(make_data())  # web + x_search
        self.assertFalse(cfg.allow_privileged_tools)
        self.assertEqual(len(cfg.specialists), 2)


class TestRequiredFields(unittest.TestCase):
    def test_missing_name(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(name=""))
        self.assertTrue(any("name" in e for e in ctx.exception.errors))

    def test_missing_synthesis(self):
        data = make_data()
        del data["synthesis"]
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(data)
        self.assertTrue(any("synthesis" in e for e in ctx.exception.errors))

    def test_synthesis_missing_model(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(synthesis={"provider": "openrouter"}))
        self.assertTrue(any("synthesis.model" in e for e in ctx.exception.errors))

    def test_specialist_missing_provider_and_model(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[{"role": "web", "toolset": ["web"]}]))
        errors = ctx.exception.errors
        self.assertTrue(any("provider" in e for e in errors))
        self.assertTrue(any("model" in e for e in errors))

    def test_empty_specialists(self):
        with self.assertRaises(ConfigError):
            FleetConfig.from_dict(make_data(specialists=[]))


class TestErrorAccumulation(unittest.TestCase):
    def test_reports_all_errors_in_one_raise(self):
        # missing name AND a specialist missing provider+model -> at least 3 errors.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(name="", specialists=[{"role": "x"}]))
        self.assertGreaterEqual(len(ctx.exception.errors), 3)


class TestPrivilegedToolsetGate(unittest.TestCase):
    def test_privileged_without_optin_errors(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[
                {"role": "coder", "provider": "openrouter", "model": "m", "toolset": ["code_execution"]},
            ]))
        self.assertTrue(any("privileged" in e for e in ctx.exception.errors))

    def test_privileged_with_optin_ok(self):
        cfg = FleetConfig.from_dict(make_data(
            allow_privileged_tools=True,
            specialists=[
                {"role": "coder", "provider": "openrouter", "model": "m",
                 "toolset": ["code_execution", "web"], "focus": "write code"},
            ],
        ))
        self.assertTrue(cfg.allow_privileged_tools)
        self.assertIn("code_execution", cfg.specialists[0].toolset)

    def test_browser_rejected_without_optin(self):
        # `browser` is a real Hermes toolset granting full browser automation
        # (navigate/click/type/CDP) — the untrusted-content -> action injection path.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[
                {"role": "surf", "provider": "p", "model": "m", "toolset": ["web", "browser"]},
            ]))
        self.assertTrue(any("browser" in e for e in ctx.exception.errors))

    def test_browser_allowed_with_optin(self):
        cfg = FleetConfig.from_dict(make_data(
            allow_privileged_tools=True,
            specialists=[{"role": "surf", "provider": "p", "model": "m",
                          "toolset": ["browser"], "focus": "browse web"}],
        ))
        self.assertIn("browser", cfg.specialists[0].toolset)

    def test_debugging_composite_rejected_without_optin(self):
        # `debugging` is a composite that expands to web+file+terminal: a denylist
        # of literal names would wave it through. Fail-closed must reject it.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[
                {"role": "dbg", "provider": "p", "model": "m", "toolset": ["debugging"]},
            ]))
        self.assertTrue(any("debugging" in e for e in ctx.exception.errors))

    def test_unknown_toolset_rejected_without_optin(self):
        # `code` is NOT a real Hermes toolset (it silently grants nothing there);
        # fail-closed turns that typo into a loud error instead of a no-op lane.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[
                {"role": "x", "provider": "p", "model": "m", "toolset": ["code"]},
            ]))
        self.assertTrue(any("code" in e for e in ctx.exception.errors))

    def test_safe_content_toolsets_need_no_optin(self):
        # Lock the allowlist: representative read/search/generate toolsets pass
        # without the privileged opt-in.
        cfg = FleetConfig.from_dict(make_data(
            specialists=[
                {"role": "a", "provider": "p", "model": "m",
                 "toolset": ["web", "search", "x_search"], "focus": "web search"},
                {"role": "b", "provider": "p", "model": "m",
                 "toolset": ["vision", "image_gen", "tts"], "focus": "visual analysis"},
            ],
        ))
        self.assertFalse(cfg.allow_privileged_tools)
        self.assertEqual(len(cfg.specialists), 2)


class TestDuplicateRole(unittest.TestCase):
    def test_duplicate_role_errors(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[
                {"role": "web", "provider": "p", "model": "m1"},
                {"role": "web", "provider": "p", "model": "m2"},
            ]))
        self.assertTrue(any("duplicate" in e for e in ctx.exception.errors))

    def test_parser_hostile_role_errors(self):
        """A role with leading/trailing whitespace, '===', a C0 control, OR a
        preview-hidden char (DEL/C1, bidi, line separator) cannot round-trip as a
        judge per-lane label — the approval preview's sanitizer strips it, so the
        displayed form differs from the prompt/parsed form, silently losing every
        grade. Config rejects all of them loudly (cross-model + dedicated-Codex finding).
        \\x7f is DEL (Cc); \\u202e is a right-to-left-override bidi control (Cf) —
        both invisible/stripped on the approval surface."""
        for bad in (" security ", "red\nteam", "a===b", "sec\x7frole",
                    "sec" + chr(0x202E) + "role"):
            with self.assertRaises(ConfigError) as ctx:
                FleetConfig.from_dict(make_data(specialists=[
                    {"role": bad, "provider": "p", "model": "m", "focus": "x"},
                ]))
            self.assertTrue(
                any("must not contain" in e for e in ctx.exception.errors),
                f"expected a role-hygiene error for {bad!r}, got {ctx.exception.errors}",
            )

    def test_clean_kebab_role_is_valid(self):
        """A normal role (internal hyphens/dots, no padding) is unaffected by the
        hygiene check — existing fleets keep parsing."""
        cfg = FleetConfig.from_dict(make_data(specialists=[
            {"role": "security-reviewer.v2", "provider": "p", "model": "m", "focus": "x"},
        ]))
        self.assertEqual(cfg.specialists[0].role, "security-reviewer.v2")


class TestMalformedEntries(unittest.TestCase):
    def test_role_not_a_string_does_not_crash(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[
                {"role": ["web"], "provider": "p", "model": "m"},
            ]))
        self.assertTrue(any("role" in e for e in ctx.exception.errors))

    def test_toolset_element_not_a_string_does_not_crash(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[
                {"role": "web", "provider": "p", "model": "m", "toolset": [{"x": "y"}]},
            ]))
        self.assertTrue(any("toolset" in e and "string" in e for e in ctx.exception.errors))

    def test_toolset_not_a_list(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[
                {"role": "web", "provider": "p", "model": "m", "toolset": "web"},
            ]))
        self.assertTrue(any("toolset must be a list" in e for e in ctx.exception.errors))

    def test_specialist_not_a_mapping_keeps_accumulating(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(specialists=[42, {"role": "web"}]))
        errs = ctx.exception.errors
        self.assertTrue(any("must be a mapping" in e for e in errs))
        self.assertTrue(any("provider" in e for e in errs))  # second entry still validated

    def test_synthesis_missing_provider(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(synthesis={"model": "m"}))
        self.assertTrue(any("synthesis.provider" in e for e in ctx.exception.errors))


class TestPrivilegedGateTypeSafety(unittest.TestCase):
    def test_quoted_false_is_rejected_not_truthy(self):
        # allow_privileged_tools: "false" must not silently enable the gate.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(
                allow_privileged_tools="false",
                specialists=[{"role": "coder", "provider": "p", "model": "m",
                              "toolset": ["code_execution"]}],
            ))
        self.assertTrue(any("boolean" in e for e in ctx.exception.errors))


class TestConvergenceField(unittest.TestCase):
    def test_absent_convergence_defaults_to_synthesize(self):
        cfg = FleetConfig.from_dict(make_data())
        self.assertEqual(cfg.convergence, "synthesize")

    def test_existing_fleet_parses_unchanged(self):
        # Back-compat: make_data() has no convergence key; all other fields identical.
        cfg = FleetConfig.from_dict(make_data())
        self.assertEqual(cfg.name, "test-swarm")
        self.assertIsInstance(cfg.synthesis, SynthesisSpec)
        self.assertEqual(len(cfg.specialists), 2)
        self.assertFalse(cfg.allow_privileged_tools)

    def test_collect_with_no_synthesis_is_valid(self):
        data = make_data(convergence="collect")
        del data["synthesis"]
        cfg = FleetConfig.from_dict(data)
        self.assertIsNone(cfg.synthesis)
        self.assertEqual(cfg.convergence, "collect")

    def test_synthesize_with_no_synthesis_still_errors(self):
        data = make_data()
        del data["synthesis"]
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(data)
        self.assertTrue(any("synthesis" in e for e in ctx.exception.errors))

    def test_synthesize_explicit_with_no_synthesis_errors(self):
        data = make_data(convergence="synthesize")
        del data["synthesis"]
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(data)
        self.assertTrue(any("synthesis" in e for e in ctx.exception.errors))

    def test_bogus_convergence_accumulates_error(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(convergence="bogus"))
        self.assertTrue(any("convergence" in e for e in ctx.exception.errors))

    def test_non_string_convergence_accumulates_error_not_typeerror(self):
        # A non-str (unhashable) convergence must accumulate a ConfigError, never raise
        # TypeError on the set-membership check (the loader's malformed-input contract).
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(convergence=["collect"]))
        self.assertTrue(any("convergence" in e for e in ctx.exception.errors))

    def test_collect_privileged_toolset_still_requires_optin(self):
        data = {
            "name": "collect-fleet",
            "convergence": "collect",
            "specialists": [
                {"role": "coder", "provider": "p", "model": "m", "toolset": ["code_execution"]},
            ],
        }
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(data)
        self.assertTrue(any("privileged" in e for e in ctx.exception.errors))

    def test_collect_empty_toolset_survives_as_empty_list(self):
        data = {
            "name": "collect-fleet",
            "convergence": "collect",
            "specialists": [
                {"role": "scan", "provider": "p", "model": "m", "toolset": [], "focus": "analysis only"},
            ],
        }
        cfg = FleetConfig.from_dict(data)
        self.assertEqual(cfg.specialists[0].toolset, [])

    def test_collect_with_synthesis_block_is_valid(self):
        # A collect fleet that also carries a synthesis: block must not error.
        cfg = FleetConfig.from_dict(make_data(convergence="collect"))
        self.assertEqual(cfg.convergence, "collect")
        # synthesis block was present in make_data(); it should be parsed, not errored
        self.assertIsNotNone(cfg.synthesis)


class TestTopologyField(unittest.TestCase):
    def test_absent_topology_defaults_to_parallel(self):
        # R1: omitting topology parses cleanly and yields the default.
        cfg = FleetConfig.from_dict(make_data())
        self.assertEqual(cfg.topology, "parallel")

    def test_sequential_and_collect_set_independently(self):
        # R2: both axes settable on one fleet; neither constrains the other.
        data = make_data(convergence="collect")
        del data["synthesis"]
        data["topology"] = "sequential"
        cfg = FleetConfig.from_dict(data)
        self.assertEqual(cfg.topology, "sequential")
        self.assertEqual(cfg.convergence, "collect")

    def test_bogus_topology_accumulates_error(self):
        # R3: invalid topology value → ConfigError, error names the problem.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(topology="diagonal"))
        self.assertTrue(any("topology" in e for e in ctx.exception.errors))

    def test_topology_error_accumulates_with_convergence_error(self):
        # R3 / accumulating path: a bad topology AND a bad convergence both appear
        # in the single raised ConfigError — proving the accumulating-error path.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(topology="diagonal", convergence="bogus"))
        errors = ctx.exception.errors
        self.assertTrue(any("topology" in e for e in errors))
        self.assertTrue(any("convergence" in e for e in errors))
        self.assertGreaterEqual(len(errors), 2)

    def test_non_string_topology_accumulates_error_not_typeerror(self):
        # isinstance guard: a list value for topology must not raise TypeError on the
        # set membership test — the loader's malformed-input-never-tracebacks contract.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(topology=["sequential"]))
        self.assertTrue(any("topology" in e for e in ctx.exception.errors))


class TestDescriptionField(unittest.TestCase):
    def test_description_parses_when_present(self):
        cfg = FleetConfig.from_dict(make_data(description="A research fleet"))
        self.assertEqual(cfg.description, "A research fleet")

    def test_description_defaults_to_empty_string_when_absent(self):
        cfg = FleetConfig.from_dict(make_data())
        self.assertEqual(cfg.description, "")

    def test_description_explicit_null_renders_empty_not_none(self):
        # `description:` present but empty parses as YAML null (None); must map to
        # "" not the literal string "None" (which str(None) would produce).
        cfg = FleetConfig.from_dict(make_data(description=None))
        self.assertEqual(cfg.description, "")


class TestFleetConfigConstruction(unittest.TestCase):
    def test_construction_is_keyword_only(self):
        # FleetConfig is @dataclass(kw_only=True): positional construction must
        # raise, so a future field-order change can't silently misassign at a
        # call site. Every real construction site passes kwargs.
        spec = SpecialistSpec(role="r", provider="p", model="m")
        with self.assertRaises(TypeError):
            FleetConfig("name", [spec])  # type: ignore[misc]


class TestLoad(unittest.TestCase):
    def test_load_non_mapping_yaml(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write("- a\n- b\n")
        self.addCleanup(os.unlink, path)
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.load(path)
        self.assertTrue(any("mapping" in e for e in ctx.exception.errors))

    def test_load_syntactically_invalid_yaml(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write("name: [unterminated\n")  # invalid YAML flow sequence
        self.addCleanup(os.unlink, path)
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.load(path)
        self.assertTrue(any("parse YAML" in e for e in ctx.exception.errors))


def _make_specialist(**overrides):
    """Minimal valid specialist dict with focus; override per test."""
    spec = {"role": "web", "provider": "p", "model": "m", "focus": "find sources"}
    spec.update(overrides)
    return spec


class TestPersonaXORFocus(unittest.TestCase):
    """persona / focus XOR invariant: exactly one instruction source per specialist (R4, R5, KTD5)."""

    def test_focus_only_parses(self):
        # R4: existing focus-only specialists parse unchanged.
        cfg = FleetConfig.from_dict(make_data())
        for spec in cfg.specialists:
            self.assertTrue(spec.focus)
            self.assertEqual(spec.persona, "")

    def test_persona_only_parses(self):
        # R4: a persona-only specialist (no focus) parses cleanly.
        cfg = FleetConfig.from_dict(make_data(
            specialists=[_make_specialist(persona="adversarial-document-reviewer", focus="")],
        ))
        self.assertEqual(cfg.specialists[0].persona, "adversarial-document-reviewer")
        self.assertEqual(cfg.specialists[0].focus, "")
        self.assertEqual(cfg.specialists[0].effective_instruction, "")  # U2 populates this

    def _pop_focus(self, d):
        """Remove focus key from a specialist dict (simulates omitting it entirely)."""
        d.pop("focus", None)
        return d

    def test_both_persona_and_focus_errors(self):
        # R4 / KTD5: both non-empty → accumulate error.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(
                specialists=[_make_specialist(persona="coherence-reviewer", focus="check coherence")],
            ))
        self.assertTrue(any("both" in e for e in ctx.exception.errors))

    def test_neither_persona_nor_focus_errors(self):
        # R5 / KTD5: neither set → accumulate error.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(
                specialists=[_make_specialist(focus="", persona="")],
            ))
        self.assertTrue(any("neither" in e for e in ctx.exception.errors))

    def test_empty_focus_with_no_persona_errors(self):
        # KTD5: empty-string focus counts as absent; a specialist with focus="" and
        # no persona fails the neither-set check, not the both-set check.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(
                specialists=[{"role": "w", "provider": "p", "model": "m", "focus": ""}],
            ))
        errors = ctx.exception.errors
        self.assertTrue(any("neither" in e for e in errors))
        self.assertFalse(any("both" in e for e in errors))

    def test_persona_field_parsed_onto_spec(self):
        # The persona name is stored on the SpecialistSpec after parsing.
        cfg = FleetConfig.from_dict(make_data(
            specialists=[_make_specialist(persona="adversarial-document-reviewer", focus="")],
        ))
        self.assertEqual(cfg.specialists[0].persona, "adversarial-document-reviewer")

    def test_focus_only_effective_instruction_populated_at_parse(self):
        # A focus-only spec carries effective_instruction = focus straight from parse —
        # no resolver run needed, so a focus-only fleet runs from FleetConfig.load() alone.
        # (A persona spec stays empty until the resolver reads the file — see
        # test_persona_only_parses.)
        cfg = FleetConfig.from_dict(make_data())
        for spec in cfg.specialists:
            self.assertTrue(spec.focus, "make_data builds focus-only specs")
            self.assertEqual(spec.effective_instruction, spec.focus)

    def test_non_string_persona_accumulates_error(self):
        # isinstance-guard: a list value for persona must not TypeError through re.fullmatch.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(
                specialists=[{"role": "w", "provider": "p", "model": "m",
                              "persona": ["a-persona"], "focus": ""}],
            ))
        self.assertTrue(any("persona" in e for e in ctx.exception.errors))

    def test_whitespace_only_focus_with_no_persona_errors(self):
        # Whitespace-only focus counts as absent — same as empty-string.
        # A specialist with focus="   " and no persona triggers the "neither set" error.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(
                specialists=[{"role": "w", "provider": "p", "model": "m", "focus": "   "}],
            ))
        errors = ctx.exception.errors
        self.assertTrue(any("neither" in e for e in errors))
        self.assertFalse(any("both" in e for e in errors))

    def test_non_string_focus_accumulates_error(self):
        # isinstance-guard: a non-string focus (e.g. a list) must produce a ConfigError,
        # not propagate through str() silently as a stringified list.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(
                specialists=[{"role": "w", "provider": "p", "model": "m", "focus": []}],
            ))
        self.assertTrue(any("focus" in e for e in ctx.exception.errors))


class TestPersonaNameAllowlist(unittest.TestCase):
    """Persona name must match [A-Za-z0-9][A-Za-z0-9._-]* (R10, KTD4)."""

    def _error_names(self, names):
        """Assert each name produces a ConfigError mentioning the name."""
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(ConfigError) as ctx:
                    FleetConfig.from_dict(make_data(
                        specialists=[_make_specialist(persona=name, focus="")],
                    ))
                errors = ctx.exception.errors
                self.assertTrue(
                    any("persona" in e for e in errors),
                    f"name {name!r}: expected a persona error, got: {errors}",
                )

    def _ok_names(self, names):
        """Assert each name parses without error."""
        for name in names:
            with self.subTest(name=name):
                cfg = FleetConfig.from_dict(make_data(
                    specialists=[_make_specialist(persona=name, focus="")],
                ))
                self.assertEqual(cfg.specialists[0].persona, name)

    def test_invalid_names_error(self):
        # R10: bare `.`, `..`, leading-dot hidden names, separators, absolute paths,
        # spaces are all rejected.
        self._error_names([".", "..", ".hidden", "../x", "/etc/passwd", "a/b", "a\\b", "a b"])

    def test_valid_names_parse(self):
        # Descriptive names used for real personas must pass.
        self._ok_names(["adversarial-document-reviewer", "a.b_c-1", "Coherence1", "x"])

    def test_multiple_bad_fields_accumulate(self):
        # Accumulation: both persona-name error AND neither-set check (persona="" focus="")
        # or both-set check surface together — proving accumulate-not-fail-fast.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(
                name="",  # also bad
                specialists=[_make_specialist(persona="/absolute/path", focus="")],
            ))
        self.assertGreaterEqual(len(ctx.exception.errors), 2,
                                "expected ≥2 errors (name + persona allowlist), got: "
                                + str(ctx.exception.errors))


class TestJudgeConvergence(unittest.TestCase):
    """judge as a first-class third convergence mode (R1, R6, R7, R8; KTD1, KTD7)."""

    def _make_judge_data(self, **overrides):
        """Minimal valid judge fleet dict; override per test."""
        data = {
            "name": "judge-swarm",
            "convergence": "judge",
            "judge": {
                "provider": "xai",
                "model": "grok-4.5",
                "prompt": "Grade each specialist.",
            },
            "specialists": [
                {"role": "reviewer-a", "provider": "openrouter",
                 "model": "anthropic/claude-opus-4", "toolset": [],
                 "focus": "review correctness"},
                {"role": "reviewer-b", "provider": "xai", "model": "grok-4.3",
                 "toolset": [], "focus": "review security"},
            ],
        }
        data.update(overrides)
        return data

    def test_valid_judge_fleet_parses(self):
        cfg = FleetConfig.from_dict(self._make_judge_data())
        self.assertEqual(cfg.convergence, "judge")
        self.assertIsInstance(cfg.judge, JudgeSpec)
        self.assertEqual(cfg.judge.provider, "xai")
        self.assertEqual(cfg.judge.model, "grok-4.5")
        self.assertEqual(cfg.judge.prompt, "Grade each specialist.")
        # synthesis must be None in judge mode (KTD1)
        self.assertIsNone(cfg.synthesis)

    def test_judge_convergence_without_judge_block_errors(self):
        data = self._make_judge_data()
        del data["judge"]
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(data)
        errors = ctx.exception.errors
        self.assertTrue(any("judge" in e and "required" in e for e in errors))

    def test_judge_block_missing_provider_errors(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(self._make_judge_data(judge={"model": "grok-4.5"}))
        self.assertTrue(any("judge.provider" in e for e in ctx.exception.errors))

    def test_judge_block_missing_model_errors(self):
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(self._make_judge_data(judge={"provider": "xai"}))
        self.assertTrue(any("judge.model" in e for e in ctx.exception.errors))

    def test_judge_same_model_as_specialist_is_valid(self):
        # KTD7: no model-distinctness check; judge may share provider/model with a specialist.
        cfg = FleetConfig.from_dict(self._make_judge_data(
            judge={"provider": "xai", "model": "grok-4.3"},  # same as reviewer-b
        ))
        self.assertIsInstance(cfg.judge, JudgeSpec)
        self.assertEqual(cfg.judge.provider, "xai")
        self.assertEqual(cfg.judge.model, "grok-4.3")

    def test_judge_block_on_synthesize_fleet_is_ignored(self):
        # A stray judge: block on a synthesize fleet must not pollute config.judge.
        data = make_data()  # convergence=synthesize, has synthesis block
        data["judge"] = {"provider": "xai", "model": "grok-4.5"}
        cfg = FleetConfig.from_dict(data)
        self.assertEqual(cfg.convergence, "synthesize")
        self.assertIsNone(cfg.judge)
        self.assertIsNotNone(cfg.synthesis)  # synthesis block still parsed

    def test_judge_block_on_collect_fleet_is_ignored(self):
        # A stray judge: block on a collect fleet must not pollute config.judge.
        data = make_data(convergence="collect")
        data["judge"] = {"provider": "xai", "model": "grok-4.5"}
        cfg = FleetConfig.from_dict(data)
        self.assertEqual(cfg.convergence, "collect")
        self.assertIsNone(cfg.judge)

    def test_invalid_convergence_error_lists_all_three_modes(self):
        # Error message for an unknown convergence must name all three accepted values.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(convergence="rank"))
        errors = ctx.exception.errors
        self.assertTrue(any("convergence" in e for e in errors))
        self.assertTrue(any("judge" in e for e in errors))

    def test_judge_prompt_optional_defaults_to_empty(self):
        # prompt is optional; omitting it yields JudgeSpec.prompt == "".
        cfg = FleetConfig.from_dict(self._make_judge_data(
            judge={"provider": "xai", "model": "grok-4.5"},  # no prompt key
        ))
        self.assertEqual(cfg.judge.prompt, "")

    def test_judge_synthesis_stays_none_even_with_stray_synthesis_block(self):
        # In judge mode, a stray synthesis: block must not populate config.synthesis
        # (KTD1: reusing synthesis to hold a judge spec is a semantic trap).
        data = self._make_judge_data()
        data["synthesis"] = {"provider": "openrouter", "model": "anthropic/claude-opus-4"}
        cfg = FleetConfig.from_dict(data)
        self.assertEqual(cfg.convergence, "judge")
        self.assertIsNone(cfg.synthesis)
        self.assertIsNotNone(cfg.judge)


def make_iterative_data(**overrides):
    """Minimal valid iterative fleet dict with synthesis; override per test."""
    data = {
        "name": "test-debate",
        "topology": "iterative",
        "rounds": 3,
        "synthesis": {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
        },
        "specialists": [
            {"role": "alpha", "provider": "openrouter", "model": "google/gemini-3-flash",
             "toolset": ["web"], "focus": "argue position A"},
            {"role": "beta", "provider": "xai", "model": "grok-4.3",
             "toolset": [], "focus": "argue position B"},
        ],
    }
    data.update(overrides)
    return data


class TestIterativeTopology(unittest.TestCase):
    """topology: iterative + rounds validation (R1, R2, R3 for the new topology)."""

    def test_valid_iterative_config_parses(self):
        # topology: iterative with rounds: 3 → FleetConfig with correct fields.
        cfg = FleetConfig.from_dict(make_iterative_data())
        self.assertEqual(cfg.topology, "iterative")
        self.assertEqual(cfg.rounds, 3)

    def test_iterative_without_rounds_key_errors(self):
        # rounds key absent on an iterative fleet → ConfigError mentioning rounds.
        data = make_iterative_data()
        del data["rounds"]
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(data)
        self.assertTrue(any("rounds" in e for e in ctx.exception.errors))

    def test_iterative_rounds_zero_errors(self):
        # rounds: 0 is the unset sentinel — not a valid round count.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_iterative_data(rounds=0))
        self.assertTrue(any("rounds" in e for e in ctx.exception.errors))

    def test_iterative_rounds_negative_errors(self):
        # rounds: -1 is below the minimum of 1.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_iterative_data(rounds=-1))
        self.assertTrue(any("rounds" in e for e in ctx.exception.errors))

    def test_iterative_rounds_above_max_errors(self):
        # rounds: MAX_ROUNDS + 1 exceeds the ceiling.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_iterative_data(rounds=MAX_ROUNDS + 1))
        self.assertTrue(any("rounds" in e for e in ctx.exception.errors))

    def test_iterative_rounds_string_errors(self):
        # rounds: "3" (a string) is not a real int — must be rejected, not coerced.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_iterative_data(rounds="3"))
        self.assertTrue(any("rounds" in e for e in ctx.exception.errors))

    def test_iterative_rounds_bool_errors(self):
        # rounds: true is a Python bool, which is an int subclass — must be rejected
        # explicitly so `rounds: true` can't silently become rounds=1.
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_iterative_data(rounds=True))
        self.assertTrue(any("rounds" in e for e in ctx.exception.errors))

    def test_parallel_with_stray_rounds_parses_fine(self):
        # A parallel fleet carrying a stray rounds: 5 must not error; rounds is
        # parsed but ignored (not validated) for non-iterative topologies.
        cfg = FleetConfig.from_dict(make_data(rounds=5))
        self.assertEqual(cfg.topology, "parallel")
        self.assertEqual(cfg.rounds, 5)

    def test_parallel_with_malformed_rounds_ignored(self):
        # A non-iterative fleet IGNORES a malformed rounds value entirely — the field is
        # unused for parallel/sequential, so a non-int must not raise (it is coerced to
        # the 0 sentinel and never validated). (CodeRabbit + Copilot review.)
        cfg = FleetConfig.from_dict(make_data(rounds="not-an-int"))
        self.assertEqual(cfg.topology, "parallel")
        self.assertEqual(cfg.rounds, 0)

    def test_unknown_topology_error_mentions_iterative(self):
        # The error message for an unknown topology must name all three accepted values,
        # including the newly added "iterative".
        with self.assertRaises(ConfigError) as ctx:
            FleetConfig.from_dict(make_data(topology="debate"))
        errors = ctx.exception.errors
        self.assertTrue(any("topology" in e for e in errors))
        self.assertTrue(any("iterative" in e for e in errors))

    def test_iterative_empty_toolset_stays_empty_list(self):
        # []-vs-None invariant: an explicit toolset: [] on an iterative fleet must
        # remain [] after parse, never coerced to None.
        cfg = FleetConfig.from_dict(make_iterative_data(specialists=[
            {"role": "debater", "provider": "p", "model": "m",
             "toolset": [], "focus": "no tools needed"},
        ]))
        self.assertEqual(cfg.topology, "iterative")
        self.assertEqual(cfg.specialists[0].toolset, [])


if __name__ == "__main__":
    unittest.main()
