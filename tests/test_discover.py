"""Tests for cadre/discover.py — the palette auto-discovery core (U1).

All tests inject a fake inventory payload via discover_candidates(payload=...);
none touch a real Hermes install or the network. The one exception —
TestFetchInventoryImportFailure — forces the ImportError path deterministically
via sys.modules so it passes the same way whether or not hermes_cli happens to
be installed on the machine running the suite (hermeticity).

Test-first: these tests define the contract; the implementation must satisfy them.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

import cadre.discover as discover

# ---------------------------------------------------------------------------
# Fixtures — mirror the LIVE-VERIFIED payload shape (probed on the Hermes host
# 2026-07-06): {"model": ..., "provider": ..., "providers": [<row>, ...]}.
# Normal rows have no "auth_type" key at all; only the aggregator row does.
# ---------------------------------------------------------------------------


def _row(slug, name, models, *, authenticated=True, auth_type=None, **extra):
    row = {
        "authenticated": authenticated,
        "is_current": False,
        "is_user_defined": False,
        "models": models,
        "name": name,
        "slug": slug,
        "source": "test",
        "total_models": len(models) if isinstance(models, list) else 0,
    }
    if auth_type is not None:
        row["auth_type"] = auth_type
    row.update(extra)
    return row


# The live host had 7 rows: 6 real authenticated providers (one, anthropic, is
# key-authenticated rather than OAuth — no field distinguishes that) plus the
# moa aggregator (auth_type: "virtual").
LIVE_SHAPE_PAYLOAD = {
    "model": "grok-4.3",
    "provider": "xai-oauth",
    "providers": [
        _row("openai-codex", "OpenAI Codex", ["gpt-5.5-codex"]),
        _row("openrouter", "OpenRouter", ["anthropic/claude-fable-5", "deepseek/deepseek-v4-pro"]),
        _row("copilot", "GitHub Copilot", ["claude-opus-4.8", "gemini-3.5-flash"]),
        _row("anthropic", "Anthropic", ["claude-fable-5"]),
        _row("xai-oauth", "xAI Grok OAuth (SuperGrok / Premium+)", ["grok-4.3"]),
        _row("minimax-oauth", "MiniMax OAuth", ["minimax-01"]),
        _row("moa", "Mixture of Agents", ["default"], auth_type="virtual", warning="aggregator"),
    ],
}


# ---------------------------------------------------------------------------
# 1-2: happy path — grouping, order, moa exclusion, key-auth inclusion,
#      profile carried, both model-entry shapes normalize.
# ---------------------------------------------------------------------------


class TestDiscoverCandidatesHappyPath(unittest.TestCase):
    """The live 7-row shape: pairs grouped per provider in curated order, moa
    excluded, the key-authenticated anthropic row included, profile carried."""

    def test_provider_order_preserved_and_moa_excluded(self):
        result = discover.discover_candidates(payload=LIVE_SHAPE_PAYLOAD)
        provider_names = [p.provider for p in result.providers]
        self.assertEqual(
            provider_names,
            ["openai-codex", "openrouter", "copilot", "anthropic", "xai-oauth", "minimax-oauth"],
        )
        self.assertNotIn("moa", provider_names)

    def test_model_order_preserved_per_provider(self):
        result = discover.discover_candidates(payload=LIVE_SHAPE_PAYLOAD)
        openrouter = next(p for p in result.providers if p.provider == "openrouter")
        self.assertEqual(
            openrouter.models,
            ["anthropic/claude-fable-5", "deepseek/deepseek-v4-pro"],
        )

    def test_key_authenticated_provider_included(self):
        """R2: include every authenticated provider regardless of auth style —
        the anthropic row has no field distinguishing OAuth from key-auth."""
        result = discover.discover_candidates(payload=LIVE_SHAPE_PAYLOAD)
        self.assertIn("anthropic", [p.provider for p in result.providers])

    def test_hermes_home_carried_from_env(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/tmp/hermes-profile"}):
            result = discover.discover_candidates(payload=LIVE_SHAPE_PAYLOAD)
        self.assertEqual(result.hermes_home, os.path.abspath("/tmp/hermes-profile"))


class TestDiscoverCandidatesModelNormalization(unittest.TestCase):
    """Model entries normalize from both shapes the live payload can carry."""

    def test_bare_string_and_dict_entries_both_normalize(self):
        payload = {
            "providers": [
                _row("openrouter", "OpenRouter", ["bare/model", {"id": "dict/model"}]),
            ]
        }
        result = discover.discover_candidates(payload=payload)
        self.assertEqual(result.providers[0].models, ["bare/model", "dict/model"])


# ---------------------------------------------------------------------------
# 3-4: per-row parse failures — a legible refusal naming the row, never a
#      silent exclusion.
# ---------------------------------------------------------------------------


class TestDiscoverCandidatesRowParseFailures(unittest.TestCase):
    """An authenticated non-virtual row that cannot fully parse raises
    DiscoveryError naming the row — never a silent drop (R2/KTD2)."""

    def test_empty_models_list_raises_naming_row(self):
        payload = {"providers": [_row("openrouter", "OpenRouter", [])]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self.assertIn("openrouter", str(ctx.exception))

    def test_missing_models_key_raises_naming_row(self):
        row = _row("openrouter", "OpenRouter", ["x"])
        del row["models"]
        payload = {"providers": [row]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self.assertIn("openrouter", str(ctx.exception))

    def test_non_list_models_raises_naming_row(self):
        row = _row("openrouter", "OpenRouter", "not-a-list")
        payload = {"providers": [row]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self.assertIn("openrouter", str(ctx.exception))

    def test_dict_model_entry_missing_id_raises_naming_row(self):
        payload = {"providers": [_row("openrouter", "OpenRouter", [{"name": "no id here"}])]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self.assertIn("openrouter", str(ctx.exception))

    def test_unrecognized_model_entry_shape_raises_naming_row(self):
        payload = {"providers": [_row("openrouter", "OpenRouter", [123])]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self.assertIn("openrouter", str(ctx.exception))

    def test_missing_slug_raises_naming_row_by_display_name(self):
        row = _row("openrouter", "OpenRouter", ["x"])
        del row["slug"]
        payload = {"providers": [row]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self.assertIn("OpenRouter", str(ctx.exception))

    def test_blank_slug_raises(self):
        payload = {"providers": [_row("", "OpenRouter", ["x"])]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self.assertIn("OpenRouter", str(ctx.exception))

    def test_missing_slug_and_name_labels_by_index(self):
        """No slug AND no display name — the label falls back to the row index
        so the refusal still names something concrete."""
        row = _row("x", "x", ["m"])
        del row["slug"]
        del row["name"]
        payload = {"providers": [row]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self.assertIn("index 0", str(ctx.exception))


# ---------------------------------------------------------------------------
# 5: hermes_cli not importable -> DiscoveryError naming the manual fallback.
# ---------------------------------------------------------------------------


class TestFetchInventoryImportFailure(unittest.TestCase):
    """No hermes_cli on this interpreter (the real case on dev) -> DiscoveryError.

    Forces ImportError deterministically via sys.modules rather than relying on
    genuine absence, so this test's result does not change on a host where
    hermes_cli happens to be installed (hermeticity) — see KTD1.
    """

    def test_import_error_becomes_discovery_error(self):
        with patch.dict(sys.modules, {"hermes_cli.inventory": None}):
            with self.assertRaises(discover.DiscoveryError) as ctx:
                discover.discover_candidates()
        msg = str(ctx.exception)
        self.assertIn("palette-candidates", msg)
        self.assertIn("verify-palette", msg)


# ---------------------------------------------------------------------------
# 6: unauthenticated rows are skipped silently, not an error.
# ---------------------------------------------------------------------------


class TestDiscoverCandidatesSkipsUnauthenticated(unittest.TestCase):
    def test_unauthenticated_row_skipped_not_an_error(self):
        payload = {
            "providers": [
                _row("openrouter", "OpenRouter", ["x"], authenticated=False),
                _row("xai-oauth", "xAI", ["grok-4.3"]),
            ]
        }
        result = discover.discover_candidates(payload=payload)
        self.assertEqual([p.provider for p in result.providers], ["xai-oauth"])


# ---------------------------------------------------------------------------
# 7: zero non-virtual authenticated providers -> DiscoveryError, never an
#    empty success.
# ---------------------------------------------------------------------------


class TestDiscoverCandidatesZeroProviders(unittest.TestCase):
    def test_only_moa_raises(self):
        payload = {"providers": [_row("moa", "Mixture of Agents", ["default"], auth_type="virtual")]}
        with self.assertRaises(discover.DiscoveryError):
            discover.discover_candidates(payload=payload)

    def test_empty_providers_list_raises(self):
        with self.assertRaises(discover.DiscoveryError):
            discover.discover_candidates(payload={"providers": []})

    def test_all_unauthenticated_raises(self):
        payload = {"providers": [_row("xai-oauth", "xAI", ["grok-4.3"], authenticated=False)]}
        with self.assertRaises(discover.DiscoveryError):
            discover.discover_candidates(payload=payload)


# ---------------------------------------------------------------------------
# 8: top-level payload shape drift.
# ---------------------------------------------------------------------------


class TestDiscoverCandidatesTopLevelShapeDrift(unittest.TestCase):
    def test_payload_not_dict_raises(self):
        with self.assertRaises(discover.DiscoveryError):
            discover.discover_candidates(payload=["not", "a", "dict"])

    def test_providers_key_missing_raises(self):
        with self.assertRaises(discover.DiscoveryError):
            discover.discover_candidates(payload={})

    def test_providers_not_list_raises(self):
        with self.assertRaises(discover.DiscoveryError):
            discover.discover_candidates(payload={"providers": "not-a-list"})

    def test_row_not_a_mapping_raises(self):
        """A malformed row (not even a dict) must degrade to DiscoveryError,
        never an AttributeError from calling .get() on it."""
        with self.assertRaises(discover.DiscoveryError):
            discover.discover_candidates(payload={"providers": ["not-a-dict"]})


# ---------------------------------------------------------------------------
# 9: module import has no side effects — no hermes import at module level.
# ---------------------------------------------------------------------------


class TestModuleImportHasNoSideEffects(unittest.TestCase):
    def test_no_module_level_hermes_import(self):
        """cadre/discover.py must not import hermes_cli at module level (KTD1)
        — only inside _fetch_inventory's function body."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(discover))
        top_level_names = []
        for node in tree.body:  # module-level statements only, not nested in functions
            if isinstance(node, ast.Import):
                top_level_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_names.append(node.module)
        self.assertFalse(
            any(name.startswith("hermes_cli") for name in top_level_names),
            f"hermes_cli must not be imported at module level: {top_level_names}",
        )


# ---------------------------------------------------------------------------
# 10: every DiscoveryError names the manual fallback, from every raise site.
# ---------------------------------------------------------------------------


class TestDiscoveryErrorNamesFallback(unittest.TestCase):
    """Every DiscoveryError, from every failure path, names the manual
    hand-edit fallback (R10) — a discovery failure reads as a next step,
    never a dead end."""

    def _assert_names_fallback(self, exc):
        msg = str(exc)
        self.assertIn("palette-candidates", msg)
        self.assertIn("verify-palette", msg)

    def test_zero_providers(self):
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload={"providers": []})
        self._assert_names_fallback(ctx.exception)

    def test_bad_top_level_shape(self):
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload={"providers": "nope"})
        self._assert_names_fallback(ctx.exception)

    def test_bad_row_parse(self):
        payload = {"providers": [_row("openrouter", "OpenRouter", [])]}
        with self.assertRaises(discover.DiscoveryError) as ctx:
            discover.discover_candidates(payload=payload)
        self._assert_names_fallback(ctx.exception)

    def test_import_failure(self):
        with patch.dict(sys.modules, {"hermes_cli.inventory": None}):
            with self.assertRaises(discover.DiscoveryError) as ctx:
                discover.discover_candidates()
        self._assert_names_fallback(ctx.exception)


if __name__ == "__main__":
    unittest.main()
