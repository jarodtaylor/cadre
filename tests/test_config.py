import os
import tempfile
import unittest

from fleet_engine.config import ConfigError, FleetConfig, SynthesisSpec


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
                {"role": "coder", "provider": "openrouter", "model": "m", "toolset": ["code_execution", "web"]},
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
            specialists=[{"role": "surf", "provider": "p", "model": "m", "toolset": ["browser"]}],
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
                {"role": "a", "provider": "p", "model": "m", "toolset": ["web", "search", "x_search"]},
                {"role": "b", "provider": "p", "model": "m", "toolset": ["vision", "image_gen", "tts"]},
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
                {"role": "scan", "provider": "p", "model": "m", "toolset": []},
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


class TestDescriptionField(unittest.TestCase):
    def test_description_parses_when_present(self):
        cfg = FleetConfig.from_dict(make_data(description="A research fleet"))
        self.assertEqual(cfg.description, "A research fleet")

    def test_description_defaults_to_empty_string_when_absent(self):
        cfg = FleetConfig.from_dict(make_data())
        self.assertEqual(cfg.description, "")


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


if __name__ == "__main__":
    unittest.main()
