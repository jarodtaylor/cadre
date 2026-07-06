"""Tests for cadre/discover.py — the palette auto-discovery core (U1) and the
candidates writer + `cadre discover` CLI entrypoint (U2).

Most tests inject a fake inventory payload via discover_candidates(payload=...);
none touch a real Hermes install or the network. Exceptions that deliberately
force the ImportError path deterministically via sys.modules (mirroring
TestFetchInventoryImportFailure) do so precisely to stay hermetic whether or
not hermes_cli happens to be installed on the machine running the suite.

Test-first: these tests define the contract; the implementation must satisfy them.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

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


# ---------------------------------------------------------------------------
# U2: write_candidates — the discovery-owned candidates file writer.
# All tests use a tempfile.mkdtemp() path and never touch ~/.cadre.
# ---------------------------------------------------------------------------


def _one_provider_result(provider="xai-oauth", models=("grok-4.3",), hermes_home="/tmp/profile"):
    return discover.DiscoveryResult(
        providers=[discover.DiscoveredProvider(provider=provider, models=list(models))],
        hermes_home=hermes_home,
    )


class TestWriteCandidatesPermissions(unittest.TestCase):
    """write_candidates writes a 0o600 owner-only file and creates a missing parent."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_written_file_is_0600(self):
        out = self.tmp / "palette-candidates.yaml"
        discover.write_candidates(_one_provider_result(), out)
        mode = stat.S_IMODE(out.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_parent_dir_created_if_missing(self):
        nested = self.tmp / "subdir" / "deeper"
        out = nested / "palette-candidates.yaml"
        discover.write_candidates(_one_provider_result(), out)
        self.assertTrue(out.exists())


class TestWriteCandidatesSymlinkGuard(unittest.TestCase):
    """write_candidates refuses to follow a symlink planted at the destination
    (O_NOFOLLOW) — matches write_palette / write_approval / write_config."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_symlinked_destination_not_written_through(self):
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET", encoding="utf-8")
        out = self.tmp / "palette-candidates.yaml"
        out.symlink_to(sentinel)

        with self.assertRaises(OSError):
            discover.write_candidates(_one_provider_result(), out)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OPERATOR SECRET")
        self.assertTrue(out.is_symlink(), "the symlink itself must be untouched, not replaced")


class TestWriteCandidatesParentSafety(unittest.TestCase):
    """write_candidates refuses to write into a group/other-writable (or
    foreign-owned) parent directory — mirrors write_palette's same guard."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_group_writable_parent_refused(self):
        # Create the parent FIRST with the bad mode: write_candidates's
        # mkdir(exist_ok=True) tightens a freshly-CREATED dir to 0o700 but
        # does NOT downgrade a pre-existing one, so the loose mode must
        # predate the call for this test to actually exercise the guard.
        d = self.tmp / "loose"
        d.mkdir(mode=0o770)
        os.chmod(d, 0o770)  # chmod after mkdir -- mkdir's mode arg is umask-affected

        out = d / "palette-candidates.yaml"
        with self.assertRaises(OSError):
            discover.write_candidates(_one_provider_result(), out)
        self.assertFalse(out.exists(), "no file should be written into an unsafe parent")


class TestWriteCandidatesRegenerateNotice(unittest.TestCase):
    """R4: regenerating over an existing file emits a loud [cadre] stderr
    notice BEFORE overwriting; a first-time write prints no such notice —
    there's nothing to warn about losing yet (AE6's discover-regenerates half)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = self.tmp / "palette-candidates.yaml"

    def test_first_write_prints_no_overwrite_notice(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            discover.write_candidates(_one_provider_result(), self.out)
        self.assertEqual(err.getvalue(), "")

    def test_regenerate_prints_loud_notice_and_replaces_content(self):
        discover.write_candidates(_one_provider_result(provider="xai-oauth"), self.out)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            discover.write_candidates(_one_provider_result(provider="openrouter"), self.out)
        notice = err.getvalue()
        self.assertIn("[cadre]", notice)
        self.assertIn("discovery-owned", notice)
        self.assertIn("discarded", notice)

        loaded = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["candidates"], [{"provider": "openrouter", "model": "grok-4.3"}])


class TestWriteCandidatesToolsetsCarryOver(unittest.TestCase):
    """toolsets: carries over from a prior candidates file when one exists
    and parses; the packaged default is used only on first write (KTD3)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = self.tmp / "palette-candidates.yaml"

    def test_first_write_uses_packaged_default_toolsets(self):
        discover.write_candidates(_one_provider_result(), self.out)
        loaded = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        expected = discover._default_toolsets()
        self.assertEqual(loaded["toolsets"], expected)
        self.assertTrue(len(loaded["toolsets"]) > 0)

    def test_regenerate_carries_over_prior_custom_toolsets(self):
        self.out.write_text(
            "candidates:\n  - provider: old\n    model: old-model\ntoolsets: [web]\n",
            encoding="utf-8",
        )
        discover.write_candidates(_one_provider_result(), self.out)
        loaded = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["toolsets"], ["web"])

    def test_regenerate_preserves_explicit_empty_toolsets(self):
        """An explicit `toolsets: []` in the prior file is a meaningful
        operator choice, not a reason to fall back to the packaged default."""
        self.out.write_text(
            "candidates:\n  - provider: old\n    model: old-model\ntoolsets: []\n",
            encoding="utf-8",
        )
        discover.write_candidates(_one_provider_result(), self.out)
        loaded = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["toolsets"], [])

    def test_regenerate_over_unparseable_prior_file_falls_back_to_default(self):
        self.out.write_text("toolsets: [unterminated\n", encoding="utf-8")
        discover.write_candidates(_one_provider_result(), self.out)
        loaded = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["toolsets"], discover._default_toolsets())

    def test_regenerate_over_scalar_toolsets_falls_back_to_default(self):
        """A scalar `toolsets: web` (not a list) must not be char-iterated —
        same guard as verify_palette._load_candidates."""
        self.out.write_text(
            "candidates:\n  - provider: old\n    model: old-model\ntoolsets: web\n",
            encoding="utf-8",
        )
        discover.write_candidates(_one_provider_result(), self.out)
        loaded = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["toolsets"], discover._default_toolsets())


class TestWriteCandidatesCouplingWithVerifyPalette(unittest.TestCase):
    """The discover -> verify format contract: a written candidates file
    round-trips through verify_palette._load_candidates with no loss of
    pairs or order.
    (docs/solutions/design-patterns/coupling-test-for-cross-module-format-contracts.md)
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = self.tmp / "palette-candidates.yaml"

    def test_round_trips_through_load_candidates_in_order(self):
        import cadre.verify_palette as verify_palette  # local: see module docstring

        result = discover.DiscoveryResult(
            providers=[
                discover.DiscoveredProvider(provider="openrouter", models=["a/model", "b/model"]),
                discover.DiscoveredProvider(provider="xai-oauth", models=["grok-4.3"]),
            ],
            hermes_home="/tmp/profile",
        )
        discover.write_candidates(result, self.out)

        candidates, toolsets = verify_palette._load_candidates(self.out)
        self.assertEqual(
            candidates,
            [
                ("openrouter", "a/model"),
                ("openrouter", "b/model"),
                ("xai-oauth", "grok-4.3"),
            ],
        )
        self.assertEqual(toolsets, discover._default_toolsets())


class TestWriteCandidatesFileContentVerbatim(unittest.TestCase):
    """KTD9: the YAML file content is written verbatim (never sanitized) —
    a hostile provider/model string must round-trip byte-for-byte through
    write_candidates, even though it renders sanitized wherever main() prints
    it to a terminal (see TestDiscoverMainSanitizesTerminalNotFile)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_hostile_provider_round_trips_verbatim_in_file(self):
        hostile = "xai\x1b[31m-evil"
        out = self.tmp / "palette-candidates.yaml"
        discover.write_candidates(_one_provider_result(provider=hostile), out)

        loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["candidates"][0]["provider"], hostile)


# ---------------------------------------------------------------------------
# U3 (#61): write_candidates(..., create_only=True) — the setup-seeding
# write mode. Never overwrites; skips the loud regenerate notice; otherwise
# identical posture (umask, parent-safety, O_NOFOLLOW, 0600).
# ---------------------------------------------------------------------------


class TestWriteCandidatesCreateOnly(unittest.TestCase):
    """create_only=True: O_EXCL instead of O_TRUNC (KTD6)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = self.tmp / "palette-candidates.yaml"

    def test_fresh_path_writes_normally(self):
        """The happy path: no prior file -> writes succeed exactly like the
        default mode, with correct content and permissions."""
        discover.write_candidates(_one_provider_result(provider="xai-oauth"), self.out, create_only=True)
        loaded = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["candidates"], [{"provider": "xai-oauth", "model": "grok-4.3"}])
        mode = stat.S_IMODE(self.out.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_fresh_path_prints_no_notice(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            discover.write_candidates(_one_provider_result(), self.out, create_only=True)
        self.assertEqual(err.getvalue(), "")

    def test_existing_file_raises_file_exists_error(self):
        """The load-bearing behavior: create_only never overwrites."""
        self.out.write_text("OPERATOR EDIT — DO NOT DELETE\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            discover.write_candidates(_one_provider_result(), self.out, create_only=True)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "OPERATOR EDIT — DO NOT DELETE\n")

    def test_existing_file_prints_no_overwrite_notice(self):
        """create_only never prints the default mode's loud regenerate notice —
        callers own their own "preserved" messaging on FileExistsError."""
        self.out.write_text("OPERATOR EDIT\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(FileExistsError):
            discover.write_candidates(_one_provider_result(), self.out, create_only=True)
        self.assertEqual(err.getvalue(), "")

    def test_existing_symlink_raises_file_exists_error_not_followed(self):
        """A symlink planted at the destination is refused the same way a
        regular file is — O_EXCL sees the dirent regardless of what it points
        to, so the symlink target is never written through."""
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET", encoding="utf-8")
        self.out.symlink_to(sentinel)

        with self.assertRaises(FileExistsError):
            discover.write_candidates(_one_provider_result(), self.out, create_only=True)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OPERATOR SECRET")
        self.assertTrue(self.out.is_symlink(), "the symlink itself must be untouched, not replaced")

    def test_unsafe_parent_still_refused(self):
        """The _parent_is_safe guard still applies in create_only mode."""
        d = self.tmp / "loose"
        d.mkdir(mode=0o770)
        os.chmod(d, 0o770)  # chmod after mkdir -- mkdir's mode arg is umask-affected

        out = d / "palette-candidates.yaml"
        with self.assertRaises(OSError):
            discover.write_candidates(_one_provider_result(), out, create_only=True)
        self.assertFalse(out.exists(), "no file should be written into an unsafe parent")

    def test_parent_dir_still_created_if_missing(self):
        nested = self.tmp / "subdir" / "deeper"
        out = nested / "palette-candidates.yaml"
        discover.write_candidates(_one_provider_result(), out, create_only=True)
        self.assertTrue(out.exists())

    def test_default_mode_unaffected(self):
        """create_only defaults to False -- the pre-existing `cadre discover`
        overwrite posture is unchanged by adding the parameter."""
        discover.write_candidates(_one_provider_result(provider="xai-oauth"), self.out)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            discover.write_candidates(_one_provider_result(provider="openrouter"), self.out)
        self.assertIn("discovery-owned", err.getvalue())
        loaded = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["candidates"], [{"provider": "openrouter", "model": "grok-4.3"}])


# ---------------------------------------------------------------------------
# U2: `cadre discover` CLI entrypoint (discover.main()).
# ---------------------------------------------------------------------------


class TestDiscoverMainSanitizesTerminalNotFile(unittest.TestCase):
    """KTD9: a hostile provider string renders SANITIZED on main()'s stdout
    summary but is written VERBATIM into the candidates YAML file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.candidates_path = self.tmp / "palette-candidates.yaml"

    def test_hostile_provider_sanitized_on_stdout_verbatim_in_file(self):
        hostile = "xai\x1b[31m-evil"
        result = _one_provider_result(provider=hostile)
        out = io.StringIO()
        with patch.object(discover, "discover_candidates", return_value=result), \
             patch.object(discover, "_DEFAULT_CANDIDATES_PATH", self.candidates_path), \
             contextlib.redirect_stdout(out):
            code = discover.main()

        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertNotIn("\x1b", printed, "the terminal summary must be sanitized")
        self.assertIn("xai", printed)  # surrounding text still renders

        loaded = yaml.safe_load(self.candidates_path.read_text(encoding="utf-8"))
        self.assertEqual(
            loaded["candidates"][0]["provider"], hostile,
            "the file content must be written verbatim, not sanitized",
        )

    def test_success_summary_names_count_path_and_next_step(self):
        result = _one_provider_result(provider="xai-oauth", models=("grok-4.3", "grok-3"))
        out = io.StringIO()
        with patch.object(discover, "discover_candidates", return_value=result), \
             patch.object(discover, "_DEFAULT_CANDIDATES_PATH", self.candidates_path), \
             contextlib.redirect_stdout(out):
            code = discover.main()
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertIn("2", printed)  # pair count
        self.assertIn(str(self.candidates_path), printed)
        self.assertIn("verify-palette", printed)


class TestDiscoverMainImportFailure(unittest.TestCase):
    """AE2: on a host without hermes_cli (e.g. this dev laptop), `cadre
    discover` exits 1 with a message naming the manual fallback — and never
    writes/clobbers the candidates file, since discovery fails before any
    write is attempted (test scenario 8)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.candidates_path = self.tmp / "palette-candidates.yaml"

    def test_import_error_exits_1_naming_fallback(self):
        out = io.StringIO()
        with patch.dict(sys.modules, {"hermes_cli.inventory": None}), \
             patch.object(discover, "_DEFAULT_CANDIDATES_PATH", self.candidates_path), \
             contextlib.redirect_stdout(out):
            code = discover.main()
        self.assertEqual(code, 1)
        printed = out.getvalue()
        self.assertIn("palette-candidates", printed)
        self.assertIn("verify-palette", printed)

    def test_import_error_does_not_write_or_clobber_existing_file(self):
        self.candidates_path.write_text("SENTINEL: hand-edited content\n", encoding="utf-8")
        with patch.dict(sys.modules, {"hermes_cli.inventory": None}), \
             patch.object(discover, "_DEFAULT_CANDIDATES_PATH", self.candidates_path), \
             contextlib.redirect_stdout(io.StringIO()):
            code = discover.main()
        self.assertEqual(code, 1)
        self.assertEqual(
            self.candidates_path.read_text(encoding="utf-8"),
            "SENTINEL: hand-edited content\n",
        )

    def test_import_error_does_not_write_when_no_prior_file(self):
        with patch.dict(sys.modules, {"hermes_cli.inventory": None}), \
             patch.object(discover, "_DEFAULT_CANDIDATES_PATH", self.candidates_path), \
             contextlib.redirect_stdout(io.StringIO()):
            discover.main()
        self.assertFalse(self.candidates_path.exists())

    def test_failure_message_renders_sanitized(self):
        """KTD9: DiscoveryError text can embed payload-derived strings (slugs,
        entry reprs, hermes exception text) — main()'s failure sink must
        render it defanged."""
        out = io.StringIO()
        hostile = discover.DiscoveryError(
            f"boom\x1b[31m drifted. {discover._MANUAL_FALLBACK}"
        )
        with patch.object(discover, "discover_candidates", side_effect=hostile), \
             patch.object(discover, "_DEFAULT_CANDIDATES_PATH", self.candidates_path), \
             contextlib.redirect_stdout(out):
            code = discover.main()
        self.assertEqual(code, 1)
        printed = out.getvalue()
        self.assertNotIn("\x1b", printed)
        self.assertIn("drifted", printed)  # surrounding text still renders


if __name__ == "__main__":
    unittest.main()
