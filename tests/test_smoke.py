"""Tests for cadre/smoke.py -- the #79 zero-spend pre-release host smoke gate.

Hermeticity: every check is exercised through its injection seam (an injected
inventory payload for ``check_inventory_shape``, an explicit ``fleet_path``
for ``check_preflight``) or against real PACKAGED data that ships with the
repo (``check_fleet_validation``, over ``cadre/data/fleets/*.example.yaml`` --
never ``~/.cadre``). None of these tests touch a real Hermes install, the
network, or the real ``~/.cadre`` on the machine running the suite. Forced
``hermes_cli.inventory`` import failures use the same deterministic
``sys.modules`` patch tests/test_discover.py uses, so results don't depend on
whether the machine running the suite happens to have ``hermes_cli``
installed.

CLI dispatch wiring (`cadre smoke` reachable through ``cli.main()``'s argparse)
lives in ``tests/test_cli.py`` (mirroring ``TestDiscoverCommandCLIWiring``) --
this module covers the checks' own behavior, per the repo convention noted in
``tests/test_preflight.py``'s module docstring.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cadre.smoke as smoke
from cadre.config import FleetConfig
from cadre.personas import resolve
from cadre.preflight import preflight_refusal

# ---------------------------------------------------------------------------
# Fixtures -- mirror tests/test_discover.py's `_row()` helper (each test
# module in this repo defines its own, per the test_preflight.py precedent
# rather than importing a sibling test module's fixtures).
# ---------------------------------------------------------------------------


def _row(slug, name="Some Provider", models=("m",), *, authenticated=True, auth_type=None, **extra):
    row = {
        "authenticated": authenticated,
        "is_current": False,
        "is_user_defined": False,
        "models": list(models),
        "name": name,
        "slug": slug,
        "source": "test",
        "total_models": len(models),
    }
    if auth_type is not None:
        row["auth_type"] = auth_type
    row.update(extra)
    return row


def _payload(*rows):
    return {"model": "grok-4.3", "provider": "xai-oauth", "providers": list(rows)}


# ---------------------------------------------------------------------------
# check_inventory_shape -- happy path
# ---------------------------------------------------------------------------


class TestCheckInventoryShapeHappyPath(unittest.TestCase):
    def test_good_payload_passes(self):
        payload = _payload(_row("xai-oauth", models=["grok-4.3"]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.name, "inventory-shape")

    def test_multi_provider_payload_passes(self):
        payload = _payload(
            _row("openai-codex", models=["gpt-5.5-codex"]),
            _row("openrouter", models=["prov-a/model-x", "deepseek/deepseek-v4-pro"]),
        )
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)

    def test_unauthenticated_row_still_shape_checked_and_passes(self):
        """check_inventory_shape validates SHAPE, not candidate extraction --
        unlike discover_candidates, an unauthenticated row is not skipped."""
        payload = _payload(_row("openrouter", models=["x"], authenticated=False))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)


class TestCheckInventoryShapeAggregatorExempt(unittest.TestCase):
    def test_minimally_shaped_aggregator_row_exempt(self):
        aggregator = {
            "slug": "moa",
            "name": "Mixture of Agents",
            "auth_type": "virtual",
            "warning": "aggregator",
            "models": ["default"],
        }  # missing authenticated/is_current/is_user_defined/source/total_models
        payload = _payload(_row("xai-oauth", models=["grok-4.3"]), aggregator)
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)

    def test_fully_shaped_aggregator_row_also_exempt(self):
        """Mirrors the live 7-row shape from test_discover.py's
        LIVE_SHAPE_PAYLOAD -- even a fully-shaped aggregator row is skipped by
        construction, not merely tolerated by accident."""
        aggregator = _row("moa", "Mixture of Agents", ["default"], auth_type="virtual", warning="x")
        payload = _payload(_row("xai-oauth", models=["grok-4.3"]), aggregator)
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)


class TestCheckInventoryShapeDictModelEntries(unittest.TestCase):
    def test_dict_form_model_entries_accepted(self):
        payload = _payload(_row("openrouter", models=[{"id": "vendor/model"}, "bare/model"]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)


class TestCheckInventoryShapeExtraKeysTolerated(unittest.TestCase):
    def test_extra_top_level_key_tolerated_with_stderr_note(self):
        payload = _payload(_row("xai-oauth", models=["grok-4.3"]))
        payload["extra_future_field"] = "whatever"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("extra_future_field", err.getvalue())
        self.assertIn("benign", err.getvalue())

    def test_extra_row_level_key_tolerated_with_stderr_note(self):
        row = _row("xai-oauth", models=["grok-4.3"], pricing={"in": 1})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = smoke.check_inventory_shape(payload=_payload(row))
        self.assertTrue(result.ok, result.detail)
        self.assertIn("pricing", err.getvalue())

    def test_no_note_printed_when_no_extra_keys(self):
        payload = _payload(_row("xai-oauth", models=["grok-4.3"]))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            smoke.check_inventory_shape(payload=payload)
        self.assertEqual(err.getvalue(), "")


# ---------------------------------------------------------------------------
# check_inventory_shape -- drift: each case names exactly what drifted.
# ---------------------------------------------------------------------------


class TestCheckInventoryShapeDrift(unittest.TestCase):
    def test_missing_required_key_fails_naming_row_and_key(self):
        row = _row("xai-oauth", models=["grok-4.3"])
        del row["total_models"]
        result = smoke.check_inventory_shape(payload=_payload(row))
        self.assertFalse(result.ok)
        self.assertIn("xai-oauth", result.detail)
        self.assertIn("total_models", result.detail)

    def test_wrong_type_fails_naming_row_and_key(self):
        row = _row("xai-oauth", models=["grok-4.3"])
        row["authenticated"] = "yes"  # str, not bool
        result = smoke.check_inventory_shape(payload=_payload(row))
        self.assertFalse(result.ok)
        self.assertIn("xai-oauth", result.detail)
        self.assertIn("authenticated", result.detail)

    def test_bool_not_accepted_as_total_models(self):
        """bool is an int subclass in Python -- a naive isinstance(x, int)
        would silently accept a wrong-shaped boolean here."""
        row = _row("xai-oauth", models=["grok-4.3"])
        row["total_models"] = True
        result = smoke.check_inventory_shape(payload=_payload(row))
        self.assertFalse(result.ok)
        self.assertIn("total_models", result.detail)

    def test_bad_models_entry_fails_naming_row(self):
        payload = _payload(_row("xai-oauth", models=[123]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("xai-oauth", result.detail)

    def test_dict_model_entry_missing_id_fails(self):
        payload = _payload(_row("openrouter", models=[{"name": "no id here"}]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("openrouter", result.detail)

    def test_missing_top_level_key_fails_naming_it(self):
        payload = _payload(_row("xai-oauth", models=["grok-4.3"]))
        del payload["model"]
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("model", result.detail)

    def test_providers_not_a_list_fails(self):
        payload = _payload()
        payload["providers"] = "nope"
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("providers", result.detail)

    def test_row_not_a_mapping_fails(self):
        payload = _payload()
        payload["providers"] = ["not-a-dict"]
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("index 0", result.detail)

    def test_payload_not_a_mapping_fails(self):
        result = smoke.check_inventory_shape(payload=["not", "a", "dict"])
        self.assertFalse(result.ok)
        self.assertIn("mapping", result.detail)


# ---------------------------------------------------------------------------
# check_inventory_shape -- alignment with discover_candidates(): any payload
# shape that would make discover_candidates() raise DiscoveryError on an
# authenticated row must FAIL here too (Copilot review, PR #83) -- an empty
# slug, an empty models list, and an empty-string/empty-id model entry were
# all previously accepted as "shape-valid" even though discover_candidates()
# rejects each of them once a row is authenticated. Unauthenticated rows are
# deliberately left un-tightened (regression tests below), since
# discover_candidates() never reads their slug/models content at all.
# ---------------------------------------------------------------------------


class TestCheckInventoryShapeDiscoverAlignment(unittest.TestCase):
    def test_authenticated_empty_slug_fails(self):
        payload = _payload(_row("", models=["grok-4.3"]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("empty slug", result.detail)

    def test_authenticated_empty_models_list_fails(self):
        payload = _payload(_row("xai-oauth", models=[]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("xai-oauth", result.detail)
        self.assertIn("empty models list", result.detail)

    def test_authenticated_empty_string_model_entry_fails(self):
        payload = _payload(_row("xai-oauth", models=[""]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("xai-oauth", result.detail)
        self.assertIn("empty-string model entry", result.detail)

    def test_authenticated_empty_id_dict_model_entry_fails(self):
        payload = _payload(_row("openrouter", models=[{"id": ""}]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertFalse(result.ok)
        self.assertIn("openrouter", result.detail)
        self.assertIn("empty-string model entry", result.detail)

    def test_whitespace_only_slug_on_authenticated_row_still_passes(self):
        """discover_candidates()'s own emptiness check is `not slug`, which a
        whitespace-only string does not trip (it's truthy) -- smoke must
        reject EXACTLY what discover rejects, not invent extra strictness via
        e.g. a .strip() discover itself never applies."""
        payload = _payload(_row("  ", models=["grok-4.3"]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)

    def test_whitespace_only_model_entry_on_authenticated_row_still_passes(self):
        payload = _payload(_row("xai-oauth", models=["  "]))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)


class TestCheckInventoryShapeDiscoverAlignmentUnauthenticatedRegression(unittest.TestCase):
    """discover_candidates() skips an unauthenticated row entirely (`if not
    row.get("authenticated"): continue`) before it ever reads that row's
    slug/models -- so an unauthenticated row with the exact same empty
    content that fails an authenticated row must keep PASSING smoke's
    pure-shape check, unchanged by the #83 tightening above."""

    def test_unauthenticated_empty_slug_still_passes(self):
        payload = _payload(_row("", models=["x"], authenticated=False))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)

    def test_unauthenticated_empty_models_list_still_passes(self):
        payload = _payload(_row("openrouter", models=[], authenticated=False))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)

    def test_unauthenticated_empty_string_model_entry_still_passes(self):
        payload = _payload(_row("openrouter", models=[""], authenticated=False))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)

    def test_unauthenticated_empty_id_dict_model_entry_still_passes(self):
        payload = _payload(_row("openrouter", models=[{"id": ""}], authenticated=False))
        result = smoke.check_inventory_shape(payload=payload)
        self.assertTrue(result.ok, result.detail)


class TestCheckInventoryShapeHermesNotImportable(unittest.TestCase):
    """No payload injected -> a real fetch is attempted -> fails gracefully on
    a machine without hermes_cli. Forced deterministically via sys.modules
    (mirrors tests/test_discover.py's TestFetchInventoryImportFailure) so this
    result does not depend on whether hermes_cli happens to be installed on
    the machine running the suite."""

    def test_import_failure_fails_gracefully_naming_hermes(self):
        with patch.dict(sys.modules, {"hermes_cli.inventory": None}):
            result = smoke.check_inventory_shape()
        self.assertFalse(result.ok)
        self.assertIn("hermes not importable", result.detail)

    def test_import_failure_does_not_raise(self):
        """The verb must still WORK (the other two checks can still run) --
        never propagate a raw exception out of the check."""
        with patch.dict(sys.modules, {"hermes_cli.inventory": None}):
            try:
                result = smoke.check_inventory_shape()
            except Exception as exc:  # noqa: BLE001 -- this is exactly what must NOT happen
                self.fail(f"check_inventory_shape raised instead of failing gracefully: {exc}")
        self.assertIsInstance(result, smoke.SmokeCheck)


class TestModuleImportHasNoSideEffects(unittest.TestCase):
    def test_no_module_level_hermes_import(self):
        """cadre/smoke.py must not import hermes_cli at module level -- it
        delegates entirely to discover._fetch_inventory's own lazy import,
        called only from inside check_inventory_shape's function body."""
        tree = ast.parse(inspect.getsource(smoke))
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
# check_fleet_validation -- the packaged starter fleets, in-process.
# ---------------------------------------------------------------------------


class TestCheckFleetValidation(unittest.TestCase):
    def test_all_packaged_starter_fleets_validate(self):
        result = smoke.check_fleet_validation()
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.name, "fleet-validate")

    def test_resolves_against_packaged_persona_pool_not_host(self):
        """The regression this check exists to catch: doc-review.example.yaml
        references personas by name; validating it must not depend on
        ~/.cadre/personas (or CADRE_PERSONAS_DIR) existing/resolving on the
        invoking host -- checks 1/3 already cover live host state; this one
        is package-integrity."""
        with patch.dict(os.environ, {"CADRE_PERSONAS_DIR": "/definitely/does/not/exist/anywhere"}):
            result = smoke.check_fleet_validation()
        self.assertTrue(result.ok, result.detail)

    def test_no_packaged_fleets_found_fails(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty)
        with patch.object(smoke, "fleets_dir", return_value=empty):
            result = smoke.check_fleet_validation()
        self.assertFalse(result.ok)
        self.assertIn("no packaged starter fleets", result.detail)

    def test_broken_fleet_fails_naming_file_and_reason(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        broken = tmp / "broken.example.yaml"
        broken.write_text("name: broken\nspecialists: []\n", encoding="utf-8")  # empty specialists -> ConfigError
        with patch.object(smoke, "fleets_dir", return_value=tmp):
            result = smoke.check_fleet_validation()
        self.assertFalse(result.ok)
        self.assertIn("broken.example.yaml", result.detail)

    def test_one_broken_fleet_among_others_names_only_the_broken_one(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "good.example.yaml").write_text(
            "name: good\n"
            "synthesis: {provider: p, model: m}\n"
            "specialists:\n"
            "  - {role: r, provider: p, model: m, toolset: [], focus: f}\n",
            encoding="utf-8",
        )
        (tmp / "broken.example.yaml").write_text("name: broken\nspecialists: []\n", encoding="utf-8")
        with patch.object(smoke, "fleets_dir", return_value=tmp):
            result = smoke.check_fleet_validation()
        self.assertFalse(result.ok)
        self.assertIn("broken.example.yaml", result.detail)
        self.assertNotIn("good.example.yaml", result.detail)


# ---------------------------------------------------------------------------
# check_preflight -- the generated palette fleet against the live palette.
# ---------------------------------------------------------------------------


class TestCheckPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _write_fleet(self, *, provider="xai-oauth", model="grok-4.3"):
        path = self.tmp / "palette-fleet.yaml"
        path.write_text(
            "name: palette-fleet\n"
            "convergence: collect\n"
            "specialists:\n"
            f"  - {{role: {provider}, provider: {provider}, model: {model}, toolset: [], "
            "focus: 'connectivity smoke test'}\n",
            encoding="utf-8",
        )
        return path

    def _write_palette(self, *, provider="xai-oauth", model="grok-4.3"):
        path = self.tmp / "palette.yaml"
        path.write_text(
            "generated_at: '2026-01-01T00:00:00.000000'\n"
            "models:\n"
            f"  - provider: {provider}\n"
            f"    model: {model}\n"
            "toolsets: []\n",
            encoding="utf-8",
        )
        return path

    def test_missing_fleet_file_fails_naming_remedy(self):
        missing = self.tmp / "nope.yaml"
        result = smoke.check_preflight(fleet_path=missing)
        self.assertFalse(result.ok)
        self.assertIn(str(missing), result.detail)
        self.assertIn("not found", result.detail)
        self.assertIn("cadre verify-palette", result.detail)

    def test_on_palette_fleet_passes(self):
        fleet_path = self._write_fleet()
        palette_path = self._write_palette()
        with patch.dict(os.environ, {"CADRE_PALETTE": str(palette_path)}):
            result = smoke.check_preflight(fleet_path=fleet_path)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.name, "preflight")

    def test_off_palette_fleet_fails_with_preflights_own_text_verbatim(self):
        """This module must never invent its own off-palette prose -- the FAIL
        detail must be EXACTLY what cadre.preflight.preflight_refusal itself
        produces for the identical config (a coupling check, not just a
        substring assertion)."""
        fleet_path = self._write_fleet(provider="xai-oauth", model="grok-4.3")
        palette_path = self._write_palette(provider="openrouter", model="some/other-model")
        with patch.dict(os.environ, {"CADRE_PALETTE": str(palette_path)}):
            result = smoke.check_preflight(fleet_path=fleet_path)
            cfg = FleetConfig.load(str(fleet_path))
            resolve(cfg, "/unused")  # focus-only: zero I/O regardless of pool_dir
            expected = preflight_refusal(cfg)
        self.assertFalse(result.ok)
        self.assertIsNotNone(expected)
        self.assertEqual(result.detail, expected)

    def test_absent_palette_fails_with_preflights_own_absent_wording(self):
        fleet_path = self._write_fleet()
        missing_palette = self.tmp / "no-palette-here.yaml"
        with patch.dict(os.environ, {"CADRE_PALETTE": str(missing_palette)}), \
                patch("cadre.preflight._hermes_cli_available", return_value=False), \
                patch("cadre.preflight._candidates_file_exists", return_value=False):
            result = smoke.check_preflight(fleet_path=fleet_path)
        self.assertFalse(result.ok)
        self.assertIn("no spend has occurred", result.detail)  # preflight's own absent-palette phrasing
        self.assertIn("cadre verify-palette", result.detail)

    def test_malformed_fleet_file_fails_naming_it(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("name: [unterminated\n", encoding="utf-8")
        result = smoke.check_preflight(fleet_path=bad)
        self.assertFalse(result.ok)
        self.assertIn("failed to load", result.detail)

    def test_default_path_reuses_palette_fleet_module_constant(self):
        """No independent second definition of the default path -- smoke's
        default resolves to the exact SAME path cadre.palette_fleet writes to,
        via that module's public default_fleet_path() accessor (see the
        check_preflight docstring).

        Asserts on the RESOLVED VALUE, not Python object identity: a sibling
        test module (tests/test_palette_fleet.py's
        TestModuleImportHasNoSideEffects) legitimately
        importlib.reload()s cadre.palette_fleet elsewhere in the suite, which
        rebinds its top-level function objects -- an identity check here would
        be fragile to that unrelated, correct reload rather than testing
        anything real about this module's behavior.
        """
        import cadre.palette_fleet as palette_fleet_module

        self.assertEqual(smoke.default_fleet_path(), palette_fleet_module.default_fleet_path())

    def test_string_fleet_path_accepted(self):
        fleet_path = self._write_fleet()
        palette_path = self._write_palette()
        with patch.dict(os.environ, {"CADRE_PALETTE": str(palette_path)}):
            result = smoke.check_preflight(fleet_path=str(fleet_path))  # str, not Path
        self.assertTrue(result.ok, result.detail)


# ---------------------------------------------------------------------------
# main() -- aggregation, stdout/stderr split, exit codes, sanitization.
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def _patched(self, *, inv=True, val=True, pre=True, inv_detail="", val_detail="", pre_detail=""):
        return (
            patch.object(smoke, "check_inventory_shape",
                         return_value=smoke.SmokeCheck("inventory-shape", inv, inv_detail)),
            patch.object(smoke, "check_fleet_validation",
                         return_value=smoke.SmokeCheck("fleet-validate", val, val_detail)),
            patch.object(smoke, "check_preflight",
                         return_value=smoke.SmokeCheck("preflight", pre, pre_detail)),
        )

    def test_all_pass_exits_zero_and_prints_verdict(self):
        p1, p2, p3 = self._patched()
        out, err = io.StringIO(), io.StringIO()
        with p1, p2, p3, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = smoke.main()
        self.assertEqual(code, 0)
        self.assertIn("3/3", out.getvalue())
        self.assertIn("PASS", err.getvalue())
        self.assertNotIn("FAIL", err.getvalue())

    def test_any_fail_exits_nonzero_and_names_failed_checks(self):
        p1, p2, p3 = self._patched(inv=False, inv_detail="boom")
        out, err = io.StringIO(), io.StringIO()
        with p1, p2, p3, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = smoke.main()
        self.assertEqual(code, 1)
        self.assertIn("inventory-shape", out.getvalue())
        self.assertIn("FAIL (boom)", err.getvalue())

    def test_all_fail_exits_nonzero_names_all(self):
        p1, p2, p3 = self._patched(inv=False, val=False, pre=False,
                                    inv_detail="a", val_detail="b", pre_detail="c")
        out, err = io.StringIO(), io.StringIO()
        with p1, p2, p3, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = smoke.main()
        self.assertEqual(code, 1)
        for name in ("inventory-shape", "fleet-validate", "preflight"):
            self.assertIn(name, out.getvalue())

    def test_stdout_has_no_per_check_breadcrumbs(self):
        """R9: stdout stays clean -- only the one-line verdict; the per-check
        [cadre] lines are stderr-only."""
        p1, p2, p3 = self._patched()
        out = io.StringIO()
        with p1, p2, p3, contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            smoke.main()
        self.assertNotIn("[cadre]", out.getvalue())

    def test_every_check_gets_a_stderr_line_regardless_of_outcome(self):
        p1, p2, p3 = self._patched(pre=False, pre_detail="x")
        err = io.StringIO()
        with p1, p2, p3, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            smoke.main()
        lines = err.getvalue()
        self.assertIn("inventory-shape", lines)
        self.assertIn("fleet-validate", lines)
        self.assertIn("preflight", lines)

    def test_fail_detail_sanitized_on_stderr(self):
        hostile = "boom\x1b[31mevil"
        p1, p2, p3 = self._patched(inv=False, inv_detail=hostile)
        err = io.StringIO()
        with p1, p2, p3, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            smoke.main()
        self.assertNotIn("\x1b", err.getvalue())
        self.assertIn("boom", err.getvalue())  # surrounding text still renders

    def test_multiline_fail_detail_collapses_to_spaced_single_line(self):
        """A multi-line detail (preflight refusals and ConfigError bullet lists
        are both newline-joined) must render as ONE stderr line with the join
        points SPACED, never word-jammed: single-line sanitize() DELETES a
        newline it meets, so without a collapse-to-spaces first, the last word
        of one line fuses onto the first word of the next
        (invariant-review catch: "checked.Fix:")."""
        multiline = "off-palette models cannot be checked.\nFix: run the remedy"
        p1, p2, p3 = self._patched(pre=False, pre_detail=multiline)
        err = io.StringIO()
        with p1, p2, p3, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            smoke.main()
        stderr_text = err.getvalue()
        self.assertIn("checked. Fix:", stderr_text)
        self.assertNotIn("checked.Fix:", stderr_text)
        # And it really is one line: the whole FAIL reason sits on the same
        # stderr line as its check name.
        preflight_lines = [ln for ln in stderr_text.splitlines() if "preflight" in ln]
        self.assertEqual(len(preflight_lines), 1)
        self.assertIn("Fix: run the remedy", preflight_lines[0])

    def test_real_preflight_refusal_renders_spaced_through_main(self):
        """END-TO-END version of the collapse test (invariant-review catch:
        the print-path tests all injected synthetic single-line fakes, so a
        real multi-line refusal never crossed main()'s print path): run
        main() with a REAL off-palette fleet + palette, no patched
        check_preflight, and assert the genuine preflight_refusal text
        renders spaced -- the remedy sentence must not fuse onto the
        preceding word."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        fleet = tmp / "palette-fleet.yaml"
        fleet.write_text(
            "name: palette-fleet\n"
            "convergence: collect\n"
            "specialists:\n"
            "  - {role: prov-a, provider: prov-a, model: model-x, toolset: [], focus: f}\n",
            encoding="utf-8",
        )
        palette = tmp / "palette.yaml"
        palette.write_text(
            "generated_at: '2026-01-01T00:00:00.000000'\n"
            "models:\n  - provider: prov-b\n    model: model-y\n"
            "toolsets: []\n",
            encoding="utf-8",
        )
        p_inv = patch.object(smoke, "check_inventory_shape",
                             return_value=smoke.SmokeCheck("inventory-shape", True))
        p_val = patch.object(smoke, "check_fleet_validation",
                             return_value=smoke.SmokeCheck("fleet-validate", True))
        p_pre_path = patch.object(smoke, "default_fleet_path", return_value=fleet)
        err = io.StringIO()
        with p_inv, p_val, p_pre_path, \
                patch.dict(os.environ, {"CADRE_PALETTE": str(palette)}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = smoke.main()
        self.assertEqual(code, 1)
        stderr_text = err.getvalue()
        # The real refusal names the pair and, on its own next line, the Fix
        # sentence -- after the collapse they must be space-separated, and
        # the specific fused shape the review reproduced must not appear.
        self.assertIn("model-x", stderr_text)
        self.assertIn(" Fix:", stderr_text)
        self.assertNotIn(")Fix:", stderr_text)

    def test_returns_exit_code_module_constants(self):
        """main()'s return values are the SAME ExitCode members cli.py's other
        verbs use -- no bespoke smoke-only exit code."""
        from cadre.exit_codes import ExitCode

        p1, p2, p3 = self._patched()
        with p1, p2, p3, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = smoke.main()
        self.assertEqual(code, ExitCode.SUCCESS)

        p1, p2, p3 = self._patched(inv=False, inv_detail="x")
        with p1, p2, p3, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = smoke.main()
        self.assertEqual(code, ExitCode.ERROR)


# ---------------------------------------------------------------------------
# Non-destructive guarantee: smoke writes NOTHING, anywhere.
# ---------------------------------------------------------------------------


class TestNonDestructive(unittest.TestCase):
    """None of the three checks may create or modify a single file. Verified
    by running the REAL checks (no injected fakes for check_fleet_validation
    or check_preflight; check_inventory_shape's real fetch is forced to fail
    closed via the same sys.modules trick used above, so this test's result
    doesn't depend on whether hermes_cli happens to be installed on the
    machine running the suite) under a fresh, isolated HOME, then asserting
    that HOME contains not a single file afterward."""

    def _isolated_home(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        return tmp

    def test_main_creates_zero_files_under_a_fresh_home(self):
        tmp = self._isolated_home()
        with patch.dict(os.environ, {"HOME": str(tmp)}), \
                patch.dict(sys.modules, {"hermes_cli.inventory": None}):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                smoke.main()
        created = list(tmp.rglob("*"))
        self.assertEqual(created, [], f"smoke must write nothing; found: {created}")

    def test_individual_checks_create_zero_files_under_a_fresh_home(self):
        """Same guarantee, exercised per-check (not only through main()) --
        each check function independently proves non-destructive, in case a
        future change calls one outside main()'s aggregation."""
        tmp = self._isolated_home()
        with patch.dict(os.environ, {"HOME": str(tmp)}), \
                patch.dict(sys.modules, {"hermes_cli.inventory": None}):
            inv = smoke.check_inventory_shape()
            val = smoke.check_fleet_validation()
            pre = smoke.check_preflight()
        # Sanity: prove these actually exercised the "nothing provisioned yet"
        # paths (inventory fails closed, preflight finds no fleet) rather than
        # accidentally short-circuiting some other way -- fleet-validate is
        # host-independent by design (packaged data) and passes regardless.
        self.assertFalse(inv.ok)
        self.assertTrue(val.ok, val.detail)
        self.assertFalse(pre.ok)
        created = list(tmp.rglob("*"))
        self.assertEqual(created, [], f"smoke must write nothing; found: {created}")

    def test_non_destructive_even_when_a_real_palette_fleet_is_present(self):
        """The strongest version of the guarantee: even when there IS
        something on disk to read (a real palette + palette-fleet under the
        isolated HOME), reading and preflighting it writes nothing new."""
        tmp = self._isolated_home()
        cadre_dir = tmp / ".cadre"
        fleets_dir_ = cadre_dir / "fleets"
        fleets_dir_.mkdir(parents=True)
        (cadre_dir / "palette.yaml").write_text(
            "generated_at: '2026-01-01T00:00:00.000000'\n"
            "models:\n  - provider: xai-oauth\n    model: grok-4.3\n"
            "toolsets: []\n",
            encoding="utf-8",
        )
        (fleets_dir_ / "palette-fleet.yaml").write_text(
            "name: palette-fleet\n"
            "convergence: collect\n"
            "specialists:\n"
            "  - {role: xai-oauth, provider: xai-oauth, model: grok-4.3, toolset: [], focus: f}\n",
            encoding="utf-8",
        )
        before_paths = sorted(str(p) for p in tmp.rglob("*"))
        before_contents = {
            str(p): p.read_bytes() for p in tmp.rglob("*") if p.is_file()
        }
        before_mtimes = {str(p): p.stat().st_mtime_ns for p in tmp.rglob("*")}

        err = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(tmp), "CADRE_PALETTE": str(cadre_dir / "palette.yaml")}), \
                patch.dict(sys.modules, {"hermes_cli.inventory": None}):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = smoke.main()

        after_paths = sorted(str(p) for p in tmp.rglob("*"))
        after_contents = {str(p): p.read_bytes() for p in tmp.rglob("*") if p.is_file()}
        after_mtimes = {str(p): p.stat().st_mtime_ns for p in tmp.rglob("*")}
        self.assertEqual(before_paths, after_paths, "no file should be created, removed, or renamed")
        self.assertEqual(before_contents, after_contents, "no file content should change")
        self.assertEqual(before_mtimes, after_mtimes, "no file should even be touched/rewritten in place")

        # The preflight check itself should have genuinely PASSED here (proves
        # the fixture is real and exercised, not accidentally inert) --
        # inventory-shape still fails closed (no hermes_cli), so the overall
        # exit code is still 1, but the per-check stderr line disambiguates.
        self.assertEqual(code, 1)
        self.assertIn("preflight ... PASS", err.getvalue())
        self.assertIn("inventory-shape ... FAIL", err.getvalue())


if __name__ == "__main__":
    unittest.main()
