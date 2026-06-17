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
