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


if __name__ == "__main__":
    unittest.main()
