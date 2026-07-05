"""Tests for the pure write_palette function in spikes/verify_aiagent_providers.py (U1).

All tests inject a tempfile.mkdtemp() path and never touch ~/.cadre.
The spike module is loaded by path (importlib) because spikes/ is not a package.

Test-first: these tests define the contract; the implementation must satisfy them.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Load spike module by path (spikes/ is not a package)
# ---------------------------------------------------------------------------

def _load_spike():
    spike_path = Path(__file__).resolve().parents[1] / "spikes" / "verify_aiagent_providers.py"
    spec = importlib.util.spec_from_file_location("verify_aiagent_providers", spike_path)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so @dataclass can resolve the
    # module's __dict__ (cls.__module__ lookup via sys.modules requires it).
    sys.modules["verify_aiagent_providers"] = mod
    spec.loader.exec_module(mod)
    return mod


_spike = _load_spike()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_records(*, ok_pairs=None, fail_pairs=None):
    """Build a list of VerifyRecord instances for test input.

    ok_pairs: list of (provider, model) for ok=True records.
    fail_pairs: list of (provider, model) for ok=False records.
    """
    VerifyRecord = _spike.VerifyRecord
    records = []
    for provider, model in (ok_pairs or []):
        records.append(VerifyRecord(provider=provider, model=model, ok=True))
    for provider, model in (fail_pairs or []):
        records.append(VerifyRecord(provider=provider, model=model, ok=False, detail="failed"))
    return records


SAFE_PAIRS = [
    ("xai", "grok-4.3"),
    ("openrouter", "google/gemini-3-flash"),
]
FAIL_PAIR = ("openrouter", "this/model-does-not-exist-xyz")

SAFE_TOOLSETS_SAMPLE = ["web", "search", "x_search", "vision"]
NON_SAFE_TOOLSETS = ["terminal", "browser"]


# ---------------------------------------------------------------------------
# Tests: write_palette contract
# ---------------------------------------------------------------------------


class TestWritePaletteFiltersOkRecords(unittest.TestCase):
    """write_palette writes only ok records to the palette models list."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_mixed_records_writes_only_ok_pairs(self):
        """2 ok + 1 failed → palette models contains exactly the 2 ok pairs, in order."""
        records = make_records(ok_pairs=SAFE_PAIRS, fail_pairs=[FAIL_PAIR])
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, SAFE_TOOLSETS_SAMPLE, out)

        data = yaml.safe_load(out.read_text())
        models = data["models"]
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0], {"provider": "xai", "model": "grok-4.3"})
        self.assertEqual(models[1], {"provider": "openrouter", "model": "google/gemini-3-flash"})

    def test_order_preserved_among_ok_records(self):
        """ok records appear in input order (not sorted)."""
        pairs = [
            ("openrouter", "google/gemini-3-flash"),
            ("xai", "grok-4.3"),
        ]
        records = make_records(ok_pairs=pairs)
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, ["web"], out)

        data = yaml.safe_load(out.read_text())
        models = data["models"]
        self.assertEqual(models[0]["provider"], "openrouter")
        self.assertEqual(models[1]["provider"], "xai")

    def test_all_ok_records_written(self):
        """All ok records appear when there are no failures."""
        records = make_records(ok_pairs=SAFE_PAIRS)
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, ["web"], out)

        data = yaml.safe_load(out.read_text())
        self.assertEqual(len(data["models"]), 2)


class TestWritePaletteZeroOkRecords(unittest.TestCase):
    """write_palette raises a clear error and writes no file when there are no ok records."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_zero_ok_raises_value_error(self):
        """No ok records → ValueError."""
        records = make_records(fail_pairs=[FAIL_PAIR])
        out = self.tmp / "palette.yaml"
        with self.assertRaises(ValueError):
            _spike.write_palette(records, ["web"], out)

    def test_zero_ok_error_message_mentions_providers(self):
        """The ValueError message must be informative."""
        records = make_records(fail_pairs=[FAIL_PAIR])
        out = self.tmp / "palette.yaml"
        with self.assertRaises(ValueError) as ctx:
            _spike.write_palette(records, ["web"], out)
        msg = str(ctx.exception).lower()
        self.assertIn("provider", msg)

    def test_zero_ok_no_file_written(self):
        """No palette file is created when zero ok records."""
        records = make_records(fail_pairs=[FAIL_PAIR])
        out = self.tmp / "palette.yaml"
        try:
            _spike.write_palette(records, ["web"], out)
        except ValueError:
            pass
        self.assertFalse(out.exists(), "no file should be written on zero ok records")

    def test_empty_record_list_raises_value_error(self):
        """Completely empty record list also raises ValueError."""
        out = self.tmp / "palette.yaml"
        with self.assertRaises(ValueError):
            _spike.write_palette([], ["web"], out)


class TestWritePalettePermissions(unittest.TestCase):
    """write_palette writes a 0o600 owner-only file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_palette_file_is_0o600(self):
        records = make_records(ok_pairs=SAFE_PAIRS)
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, SAFE_TOOLSETS_SAMPLE, out)

        mode = stat.S_IMODE(out.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_parent_dir_created_if_missing(self):
        """write_palette creates the parent directory if it doesn't exist."""
        nested = self.tmp / "subdir" / "deeper"
        out = nested / "palette.yaml"
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        _spike.write_palette(records, ["web"], out)
        self.assertTrue(out.exists())


class TestWritePaletteToolsetFiltering(unittest.TestCase):
    """write_palette silently drops any non-safe toolset name."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_non_safe_toolsets_excluded(self):
        """terminal, browser → silently excluded; safe names remain."""
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        mixed_toolsets = ["web", "terminal", "search", "browser", "x_search"]
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, mixed_toolsets, out)

        data = yaml.safe_load(out.read_text())
        toolsets = data["toolsets"]
        self.assertNotIn("terminal", toolsets)
        self.assertNotIn("browser", toolsets)
        self.assertIn("web", toolsets)
        self.assertIn("search", toolsets)
        self.assertIn("x_search", toolsets)

    def test_safe_toolset_order_preserved(self):
        """Safe toolsets appear in the same order they were declared."""
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        # safe names in a specific order, with non-safe interspersed
        toolsets = ["vision", "web", "terminal", "search"]
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, toolsets, out)

        data = yaml.safe_load(out.read_text())
        # non-safe "terminal" dropped; safe names in original order
        self.assertEqual(data["toolsets"], ["vision", "web", "search"])

    def test_all_non_safe_toolsets_yields_empty_list(self):
        """If all declared toolsets are non-safe, the palette toolsets list is empty."""
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, ["terminal", "browser", "file"], out)

        data = yaml.safe_load(out.read_text())
        self.assertEqual(data["toolsets"], [])

    def test_empty_toolsets_writes_empty_list(self):
        """An empty toolsets input produces an empty list in the palette."""
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, [], out)

        data = yaml.safe_load(out.read_text())
        self.assertEqual(data["toolsets"], [])


class TestWritePaletteGeneratedAt(unittest.TestCase):
    """write_palette records a generated_at ISO-8601 timestamp."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_generated_at_present(self):
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, ["web"], out)

        data = yaml.safe_load(out.read_text())
        self.assertIn("generated_at", data)

    def test_generated_at_non_empty_string(self):
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, ["web"], out)

        data = yaml.safe_load(out.read_text())
        self.assertIsInstance(data["generated_at"], str)
        self.assertTrue(len(data["generated_at"]) > 0)


class TestWritePaletteRoundTrip(unittest.TestCase):
    """yaml.safe_load of the written file yields the expected dict structure."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_round_trip_structure(self):
        """The loaded YAML has the locked schema: generated_at, models (list of dicts), toolsets."""
        records = make_records(ok_pairs=SAFE_PAIRS, fail_pairs=[FAIL_PAIR])
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, SAFE_TOOLSETS_SAMPLE, out)

        data = yaml.safe_load(out.read_text())

        # Top-level keys
        self.assertIn("generated_at", data)
        self.assertIn("models", data)
        self.assertIn("toolsets", data)

        # models is a list of {provider, model} dicts (no extra keys required)
        self.assertIsInstance(data["models"], list)
        for entry in data["models"]:
            self.assertIsInstance(entry, dict)
            self.assertIn("provider", entry)
            self.assertIn("model", entry)

        # toolsets is a list of strings
        self.assertIsInstance(data["toolsets"], list)
        for t in data["toolsets"]:
            self.assertIsInstance(t, str)

    def test_round_trip_values(self):
        """The loaded values match the inputs exactly."""
        ok_pairs = [("xai", "grok-4.3"), ("openrouter", "google/gemini-3-flash")]
        toolsets = ["web", "search"]
        records = make_records(ok_pairs=ok_pairs)
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, toolsets, out)

        data = yaml.safe_load(out.read_text())
        self.assertEqual(
            data["models"],
            [{"provider": p, "model": m} for p, m in ok_pairs],
        )
        self.assertEqual(data["toolsets"], toolsets)


# ---------------------------------------------------------------------------
# Tests: VerifyRecord dataclass presence
# ---------------------------------------------------------------------------


class TestVerifyRecord(unittest.TestCase):
    """VerifyRecord dataclass exists with the expected fields."""

    def test_verify_record_fields(self):
        VerifyRecord = _spike.VerifyRecord
        r = VerifyRecord(provider="xai", model="grok-4.3", ok=True)
        self.assertEqual(r.provider, "xai")
        self.assertEqual(r.model, "grok-4.3")
        self.assertTrue(r.ok)
        self.assertEqual(r.detail, "")  # default

    def test_verify_record_fail(self):
        VerifyRecord = _spike.VerifyRecord
        r = VerifyRecord(provider="openrouter", model="bad/model", ok=False, detail="timeout")
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "timeout")


# ---------------------------------------------------------------------------
# Tests: verify_candidates function signature (no live calls, just structure)
# ---------------------------------------------------------------------------


class TestVerifyCandidatesSignature(unittest.TestCase):
    """verify_candidates exists and accepts a list of (provider, model) tuples."""

    def test_function_exists(self):
        self.assertTrue(callable(_spike.verify_candidates))

    def test_signature_accepts_list_of_tuples(self):
        """verify_candidates(candidates) — can be called with an empty list (no live calls)."""
        import unittest.mock as mock
        # Patch _agent so no live AIAgent import is attempted
        with mock.patch.object(_spike, "_agent") as fake_agent:
            fake_agent.return_value.chat.return_value = "ok"
            result = _spike.verify_candidates([])
        self.assertIsInstance(result, list)

    def test_returns_list_of_verify_records(self):
        """With one candidate, returns a list with one VerifyRecord."""
        import unittest.mock as mock
        with mock.patch.object(_spike, "_agent") as fake_agent:
            fake_agent.return_value.chat.return_value = "ok"
            result = _spike.verify_candidates([("xai", "grok-4.3")])
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], _spike.VerifyRecord)
        self.assertEqual(result[0].provider, "xai")
        self.assertEqual(result[0].model, "grok-4.3")
        self.assertTrue(result[0].ok)

    def test_failed_call_returns_ok_false_record(self):
        """An exception from _agent.chat() results in ok=False record."""
        import unittest.mock as mock
        with mock.patch.object(_spike, "_agent") as fake_agent:
            fake_agent.return_value.chat.side_effect = RuntimeError("no auth")
            result = _spike.verify_candidates([("openrouter", "bad/model")])
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].ok)
        self.assertIn("RuntimeError", result[0].detail)


class TestWritePaletteHonestyHeader(unittest.TestCase):
    """The generated palette carries a point-of-use note that toolsets are declared,
    not tool-probed (Codex adversarial review, finding 3) — and stays valid YAML."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_header_warns_toolsets_not_probed(self):
        records = make_records(ok_pairs=SAFE_PAIRS)
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, ["web"], out)
        text = out.read_text()
        self.assertIn("NOT tool-probed", text)
        self.assertIn("ungrounded", text)

    def test_header_does_not_break_yaml_parse(self):
        records = make_records(ok_pairs=SAFE_PAIRS)
        out = self.tmp / "palette.yaml"
        _spike.write_palette(records, ["web"], out)
        data = yaml.safe_load(out.read_text())  # comments are ignored by the loader
        self.assertEqual(len(data["models"]), 2)
        self.assertEqual(data["toolsets"], ["web"])


# ---------------------------------------------------------------------------
# New tests: TestLoadCandidates (FIX 3)
# ---------------------------------------------------------------------------


class TestLoadCandidates(unittest.TestCase):
    """_load_candidates robustly handles malformed or absent seed files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _candidates_path(self, name="palette-candidates.yaml"):
        return self.tmp / name

    def test_file_absent_returns_providers_empty_toolsets(self):
        """If the seed file doesn't exist, returns (PROVIDERS, [])."""
        path = self._candidates_path("nonexistent.yaml")
        candidates, toolsets = _spike._load_candidates(path)
        self.assertEqual(candidates, _spike.PROVIDERS)
        self.assertEqual(toolsets, [])

    def test_valid_yaml_parsed(self):
        """A well-formed file returns the candidate tuples and toolsets."""
        path = self._candidates_path()
        path.write_text(
            "candidates:\n"
            "  - provider: xai\n"
            "    model: grok-4.3\n"
            "  - provider: openrouter\n"
            "    model: google/gemini-3-flash\n"
            "toolsets:\n"
            "  - web\n"
            "  - search\n",
            encoding="utf-8",
        )
        candidates, toolsets = _spike._load_candidates(path)
        self.assertEqual(candidates, [("xai", "grok-4.3"), ("openrouter", "google/gemini-3-flash")])
        self.assertEqual(toolsets, ["web", "search"])

    def test_empty_file_returns_providers_empty_toolsets(self):
        """An empty file (yaml.safe_load → None) returns (PROVIDERS, [])."""
        path = self._candidates_path()
        path.write_text("", encoding="utf-8")
        candidates, toolsets = _spike._load_candidates(path)
        self.assertEqual(candidates, _spike.PROVIDERS)
        self.assertEqual(toolsets, [])

    def test_non_mapping_root_returns_providers(self):
        """A top-level YAML list or scalar (not a mapping) returns (PROVIDERS, []) — no AttributeError."""
        for content in ("- a\n- b\n", "justascalar\n"):
            path = self._candidates_path()
            path.write_text(content, encoding="utf-8")
            candidates, toolsets = _spike._load_candidates(path)
            self.assertEqual(candidates, _spike.PROVIDERS)
            self.assertEqual(toolsets, [])

    def test_candidate_missing_model_is_skipped(self):
        """A dict missing 'model' is skipped; other entries are returned normally."""
        path = self._candidates_path()
        path.write_text(
            "candidates:\n"
            "  - provider: xai\n"
            "    model: grok-4.3\n"
            "  - provider: openrouter\n"
            "toolsets: []\n",
            encoding="utf-8",
        )
        candidates, toolsets = _spike._load_candidates(path)
        # The second entry (missing model) must be skipped — no KeyError
        self.assertEqual(candidates, [("xai", "grok-4.3")])

    def test_scalar_toolsets_returns_empty_list(self):
        """A scalar string `toolsets: web` must return [] (not ['w','e','b'])."""
        path = self._candidates_path()
        path.write_text(
            "candidates:\n"
            "  - provider: xai\n"
            "    model: grok-4.3\n"
            "toolsets: web\n",
            encoding="utf-8",
        )
        candidates, toolsets = _spike._load_candidates(path)
        self.assertEqual(toolsets, [], "scalar toolsets must not be iterated char-by-char")

    def test_candidates_null_no_crash(self):
        """candidates: null → no crash; falls back to PROVIDERS."""
        path = self._candidates_path()
        path.write_text("candidates: null\ntoolsets: [web]\n", encoding="utf-8")
        candidates, toolsets = _spike._load_candidates(path)
        self.assertEqual(candidates, _spike.PROVIDERS)
        self.assertEqual(toolsets, ["web"])

    def test_toolsets_null_no_crash(self):
        """toolsets: null → no crash; returns []."""
        path = self._candidates_path()
        path.write_text(
            "candidates:\n"
            "  - provider: xai\n"
            "    model: grok-4.3\n"
            "toolsets: null\n",
            encoding="utf-8",
        )
        candidates, toolsets = _spike._load_candidates(path)
        self.assertEqual(toolsets, [])


# ---------------------------------------------------------------------------
# New test: verify_candidates blank-response (FIX 3)
# ---------------------------------------------------------------------------


class TestVerifyCandidatesBlankResponse(unittest.TestCase):
    """verify_candidates treats blank/empty response as ok=False."""

    def test_empty_response_yields_ok_false_with_detail(self):
        """_agent.chat() returning '' → VerifyRecord(ok=False, detail='empty response')."""
        import unittest.mock as mock

        with mock.patch.object(_spike, "_agent") as fake_agent:
            fake_agent.return_value.chat.return_value = ""
            result = _spike.verify_candidates([("xai", "grok-4.3")])

        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].ok)
        self.assertEqual(result[0].detail, "empty response")


class TestStandaloneScriptImport(unittest.TestCase):
    """Regression (live dogfood, 2026-06-20): run as a standalone script
    (`python spikes/verify_aiagent_providers.py`, how install.sh invokes it),
    spikes/ is on sys.path[0] but the repo root is NOT, so write_palette's
    `from cadre.config import SAFE_TOOLSETS` raised ModuleNotFoundError.
    The module now inserts the repo root itself."""

    def test_write_palette_imports_cadre_without_repo_root_on_path(self):
        import subprocess

        repo_root = Path(__file__).resolve().parents[1]
        spike = repo_root / "spikes" / "verify_aiagent_providers.py"
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        out = tmp / "palette.yaml"
        driver = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('v', {str(spike)!r})\n"
            "m = importlib.util.module_from_spec(spec); sys.modules['v'] = m\n"
            "spec.loader.exec_module(m)\n"
            "m.write_palette([m.VerifyRecord('xai', 'grok-4.3', True)], ['web'], "
            f"{str(out)!r})\n"
            "print('OK')\n"
        )
        # `-I` (isolated): does NOT prepend cwd/'' to sys.path and ignores
        # PYTHONPATH, so cadre is importable ONLY via the spike's own
        # repo-root insertion — reproducing the standalone-script condition.
        result = subprocess.run(
            [sys.executable, "-I", "-c", driver],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")
        self.assertTrue(out.exists(), "palette should have been written")


class TestVerifyOutputSuppression(unittest.TestCase):
    """verify_candidates hides a candidate's raw provider output (the scary
    multi-line error AIAgent dumps for an unsupported model) and prints one calm
    status line instead (DX fix from the 2026-06-20 live dogfood)."""

    def test_provider_noise_is_suppressed_clean_status_shown(self):
        import contextlib as ctx
        import io as _io
        import sys as _sys
        import unittest.mock as mock

        def noisy_ok(*_a, **_k):
            print("SCARY PROVIDER STACK DUMP")
            print("❌ Non-retryable error — Aborting", file=_sys.stderr)
            return "ok"

        with mock.patch.object(_spike, "_agent") as fake_agent:
            fake_agent.return_value.chat.side_effect = noisy_ok
            out = _io.StringIO()
            with ctx.redirect_stdout(out):
                records = _spike.verify_candidates([("xai", "grok-4.3")])

        printed = out.getvalue()
        self.assertNotIn("SCARY PROVIDER STACK DUMP", printed)  # noise captured, not dumped
        self.assertIn("grok-4.3", printed)                      # calm status still shown
        self.assertTrue(records[0].ok)

    def test_skip_reason_mined_from_captured_output(self):
        """When AIAgent logs an error and returns None (its real behaviour for an
        unsupported model), the skip reason is mined from the captured output
        rather than a vague 'empty response'."""
        import contextlib as ctx
        import io as _io
        import sys as _sys
        import unittest.mock as mock

        def noisy_none(*_a, **_k):
            print("   📝 Error: HTTP 400: The requested model is not supported.", file=_sys.stderr)
            return None

        with mock.patch.object(_spike, "_agent") as fake_agent:
            fake_agent.return_value.chat.side_effect = noisy_none
            with ctx.redirect_stdout(_io.StringIO()):
                records = _spike.verify_candidates([("copilot", "claude-opus-4.5")])

        self.assertFalse(records[0].ok)
        self.assertIn("not supported", records[0].detail.lower())


if __name__ == "__main__":
    unittest.main()
