"""Tests for cadre/verify_palette.py — moved from the now-retired host-verification
spike during the U5 packaging pass (docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md),
which relocated write_palette/verify_candidates/_load_candidates (and the rest of the
host-verification flow) into cadre.verify_palette (a normal package import, not the
by-path load the spike needed — verify_palette.py lives inside the cadre package).

All tests inject a tempfile.mkdtemp() path and never touch ~/.cadre.

Test-first: these tests define the contract; the implementation must satisfy them.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import cadre.verify_palette as verify_palette


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_records(*, ok_pairs=None, fail_pairs=None):
    """Build a list of VerifyRecord instances for test input.

    ok_pairs: list of (provider, model) for ok=True records.
    fail_pairs: list of (provider, model) for ok=False records.
    """
    VerifyRecord = verify_palette.VerifyRecord
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
        verify_palette.write_palette(records, SAFE_TOOLSETS_SAMPLE, out)

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
        verify_palette.write_palette(records, ["web"], out)

        data = yaml.safe_load(out.read_text())
        models = data["models"]
        self.assertEqual(models[0]["provider"], "openrouter")
        self.assertEqual(models[1]["provider"], "xai")

    def test_all_ok_records_written(self):
        """All ok records appear when there are no failures."""
        records = make_records(ok_pairs=SAFE_PAIRS)
        out = self.tmp / "palette.yaml"
        verify_palette.write_palette(records, ["web"], out)

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
            verify_palette.write_palette(records, ["web"], out)

    def test_zero_ok_error_message_mentions_providers(self):
        """The ValueError message must be informative."""
        records = make_records(fail_pairs=[FAIL_PAIR])
        out = self.tmp / "palette.yaml"
        with self.assertRaises(ValueError) as ctx:
            verify_palette.write_palette(records, ["web"], out)
        msg = str(ctx.exception).lower()
        self.assertIn("provider", msg)

    def test_zero_ok_no_file_written(self):
        """No palette file is created when zero ok records."""
        records = make_records(fail_pairs=[FAIL_PAIR])
        out = self.tmp / "palette.yaml"
        try:
            verify_palette.write_palette(records, ["web"], out)
        except ValueError:
            pass
        self.assertFalse(out.exists(), "no file should be written on zero ok records")

    def test_empty_record_list_raises_value_error(self):
        """Completely empty record list also raises ValueError."""
        out = self.tmp / "palette.yaml"
        with self.assertRaises(ValueError):
            verify_palette.write_palette([], ["web"], out)


class TestWritePalettePermissions(unittest.TestCase):
    """write_palette writes a 0o600 owner-only file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_palette_file_is_0o600(self):
        records = make_records(ok_pairs=SAFE_PAIRS)
        out = self.tmp / "palette.yaml"
        verify_palette.write_palette(records, SAFE_TOOLSETS_SAMPLE, out)

        mode = stat.S_IMODE(out.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_parent_dir_created_if_missing(self):
        """write_palette creates the parent directory if it doesn't exist."""
        nested = self.tmp / "subdir" / "deeper"
        out = nested / "palette.yaml"
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        verify_palette.write_palette(records, ["web"], out)
        self.assertTrue(out.exists())


class TestWritePaletteSymlinkGuard(unittest.TestCase):
    """C5b: write_palette refuses to follow a symlink planted at the
    destination (O_NOFOLLOW) — matches provision.write_config /
    _seed_files / approval.write_approval's same guard."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_symlinked_destination_not_written_through(self):
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET", encoding="utf-8")
        out = self.tmp / "palette.yaml"
        out.symlink_to(sentinel)

        records = make_records(ok_pairs=SAFE_PAIRS)
        with self.assertRaises(OSError):
            verify_palette.write_palette(records, SAFE_TOOLSETS_SAMPLE, out)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OPERATOR SECRET")
        self.assertTrue(out.is_symlink(), "the symlink itself must be untouched, not replaced")


class TestWritePaletteParentSafety(unittest.TestCase):
    """CR1 (CodeRabbit, Major/Security): write_palette refuses to write into a
    group/other-writable (or foreign-owned) parent directory — mirrors
    approval.write_approval / install_skill.install_skill's same guard
    (approval._parent_is_safe). A loose/foreign-owned ~/.cadre (pre-existing,
    or on a shared host) must not be silently trusted just because the leaf
    write is O_NOFOLLOW-guarded."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_group_writable_parent_refused(self):
        # Create the parent FIRST with the bad mode: write_palette's
        # mkdir(exist_ok=True) tightens a freshly-CREATED dir to 0o700 but does
        # NOT downgrade a pre-existing one, so the loose mode must predate the
        # call for this test to actually exercise the guard.
        d = self.tmp / "loose"
        d.mkdir(mode=0o770)
        os.chmod(d, 0o770)  # chmod after mkdir — mkdir's mode arg is umask-affected

        records = make_records(ok_pairs=SAFE_PAIRS)
        out = d / "palette.yaml"
        with self.assertRaises(OSError):
            verify_palette.write_palette(records, SAFE_TOOLSETS_SAMPLE, out)
        self.assertFalse(out.exists(), "no file should be written into an unsafe parent")


class TestVerifyPaletteMainOSErrorDegrade(unittest.TestCase):
    """verify_palette.main() degrades a write_palette OSError to a clean exit 1,
    never a raw traceback — the main()-level consequence of C5b's O_NOFOLLOW,
    which now raises OSError when ~/.cadre/palette.yaml is a planted symlink
    (main previously caught only ValueError)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_symlinked_palette_path_degrades_to_exit_1_no_traceback(self):
        candidates = self.tmp / "palette-candidates.yaml"
        candidates.write_text(
            "candidates:\n  - provider: xai\n    model: grok-4.3\ntoolsets: [web]\n",
            encoding="utf-8",
        )
        # A symlink planted at the palette output -> write_palette's O_NOFOLLOW raises OSError.
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET", encoding="utf-8")
        palette = self.tmp / "palette.yaml"
        palette.symlink_to(sentinel)

        ok_records = make_records(ok_pairs=[("xai", "grok-4.3")])
        with patch.object(verify_palette, "_DEFAULT_CANDIDATES_PATH", candidates), \
             patch.object(verify_palette, "_DEFAULT_PALETTE_PATH", palette), \
             patch.object(verify_palette, "verify_candidates", return_value=ok_records), \
             contextlib.redirect_stdout(io.StringIO()):
            code = verify_palette.main()  # must NOT raise

        self.assertEqual(code, 1)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OPERATOR SECRET")
        self.assertTrue(palette.is_symlink())


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
        verify_palette.write_palette(records, mixed_toolsets, out)

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
        verify_palette.write_palette(records, toolsets, out)

        data = yaml.safe_load(out.read_text())
        # non-safe "terminal" dropped; safe names in original order
        self.assertEqual(data["toolsets"], ["vision", "web", "search"])

    def test_all_non_safe_toolsets_yields_empty_list(self):
        """If all declared toolsets are non-safe, the palette toolsets list is empty."""
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        out = self.tmp / "palette.yaml"
        verify_palette.write_palette(records, ["terminal", "browser", "file"], out)

        data = yaml.safe_load(out.read_text())
        self.assertEqual(data["toolsets"], [])

    def test_empty_toolsets_writes_empty_list(self):
        """An empty toolsets input produces an empty list in the palette."""
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        out = self.tmp / "palette.yaml"
        verify_palette.write_palette(records, [], out)

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
        verify_palette.write_palette(records, ["web"], out)

        data = yaml.safe_load(out.read_text())
        self.assertIn("generated_at", data)

    def test_generated_at_non_empty_string(self):
        records = make_records(ok_pairs=[("xai", "grok-4.3")])
        out = self.tmp / "palette.yaml"
        verify_palette.write_palette(records, ["web"], out)

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
        verify_palette.write_palette(records, SAFE_TOOLSETS_SAMPLE, out)

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
        verify_palette.write_palette(records, toolsets, out)

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
        VerifyRecord = verify_palette.VerifyRecord
        r = VerifyRecord(provider="xai", model="grok-4.3", ok=True)
        self.assertEqual(r.provider, "xai")
        self.assertEqual(r.model, "grok-4.3")
        self.assertTrue(r.ok)
        self.assertEqual(r.detail, "")  # default

    def test_verify_record_fail(self):
        VerifyRecord = verify_palette.VerifyRecord
        r = VerifyRecord(provider="openrouter", model="bad/model", ok=False, detail="timeout")
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "timeout")


# ---------------------------------------------------------------------------
# Tests: verify_candidates function signature (no live calls, just structure)
# ---------------------------------------------------------------------------


class TestVerifyCandidatesSignature(unittest.TestCase):
    """verify_candidates exists and accepts a list of (provider, model) tuples."""

    def test_function_exists(self):
        self.assertTrue(callable(verify_palette.verify_candidates))

    def test_signature_accepts_list_of_tuples(self):
        """verify_candidates(candidates) — can be called with an empty list (no live calls)."""
        import unittest.mock as mock
        # Patch _agent so no live AIAgent import is attempted
        with mock.patch.object(verify_palette, "_agent") as fake_agent:
            fake_agent.return_value.chat.return_value = "ok"
            result = verify_palette.verify_candidates([])
        self.assertIsInstance(result, list)

    def test_returns_list_of_verify_records(self):
        """With one candidate, returns a list with one VerifyRecord."""
        import unittest.mock as mock
        with mock.patch.object(verify_palette, "_agent") as fake_agent:
            fake_agent.return_value.chat.return_value = "ok"
            result = verify_palette.verify_candidates([("xai", "grok-4.3")])
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], verify_palette.VerifyRecord)
        self.assertEqual(result[0].provider, "xai")
        self.assertEqual(result[0].model, "grok-4.3")
        self.assertTrue(result[0].ok)

    def test_failed_call_returns_ok_false_record(self):
        """An exception from _agent.chat() results in ok=False record."""
        import unittest.mock as mock
        with mock.patch.object(verify_palette, "_agent") as fake_agent:
            fake_agent.return_value.chat.side_effect = RuntimeError("no auth")
            result = verify_palette.verify_candidates([("openrouter", "bad/model")])
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
        verify_palette.write_palette(records, ["web"], out)
        text = out.read_text()
        self.assertIn("NOT tool-probed", text)
        self.assertIn("ungrounded", text)

    def test_header_does_not_break_yaml_parse(self):
        records = make_records(ok_pairs=SAFE_PAIRS)
        out = self.tmp / "palette.yaml"
        verify_palette.write_palette(records, ["web"], out)
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
        candidates, toolsets = verify_palette._load_candidates(path)
        self.assertEqual(candidates, verify_palette.PROVIDERS)
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
        candidates, toolsets = verify_palette._load_candidates(path)
        self.assertEqual(candidates, [("xai", "grok-4.3"), ("openrouter", "google/gemini-3-flash")])
        self.assertEqual(toolsets, ["web", "search"])

    def test_empty_file_returns_providers_empty_toolsets(self):
        """An empty file (yaml.safe_load → None) returns (PROVIDERS, [])."""
        path = self._candidates_path()
        path.write_text("", encoding="utf-8")
        candidates, toolsets = verify_palette._load_candidates(path)
        self.assertEqual(candidates, verify_palette.PROVIDERS)
        self.assertEqual(toolsets, [])

    def test_non_mapping_root_returns_providers(self):
        """A top-level YAML list or scalar (not a mapping) returns (PROVIDERS, []) — no AttributeError."""
        for content in ("- a\n- b\n", "justascalar\n"):
            path = self._candidates_path()
            path.write_text(content, encoding="utf-8")
            candidates, toolsets = verify_palette._load_candidates(path)
            self.assertEqual(candidates, verify_palette.PROVIDERS)
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
        candidates, toolsets = verify_palette._load_candidates(path)
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
        candidates, toolsets = verify_palette._load_candidates(path)
        self.assertEqual(toolsets, [], "scalar toolsets must not be iterated char-by-char")

    def test_candidates_null_no_crash(self):
        """candidates: null → no crash; falls back to PROVIDERS."""
        path = self._candidates_path()
        path.write_text("candidates: null\ntoolsets: [web]\n", encoding="utf-8")
        candidates, toolsets = verify_palette._load_candidates(path)
        self.assertEqual(candidates, verify_palette.PROVIDERS)
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
        candidates, toolsets = verify_palette._load_candidates(path)
        self.assertEqual(toolsets, [])


# ---------------------------------------------------------------------------
# New test: verify_candidates blank-response (FIX 3)
# ---------------------------------------------------------------------------


class TestVerifyCandidatesBlankResponse(unittest.TestCase):
    """verify_candidates treats blank/empty response as ok=False."""

    def test_empty_response_yields_ok_false_with_detail(self):
        """_agent.chat() returning '' → VerifyRecord(ok=False, detail='empty response')."""
        import unittest.mock as mock

        with mock.patch.object(verify_palette, "_agent") as fake_agent:
            fake_agent.return_value.chat.return_value = ""
            result = verify_palette.verify_candidates([("xai", "grok-4.3")])

        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].ok)
        self.assertEqual(result[0].detail, "empty response")


class TestVerifyCandidatesSanitizedStatusLines(unittest.TestCase):
    """KTD9 sibling-sink: the per-candidate ✓/✗ lines render discovered
    provider/model strings (and the failure reason mined from provider output),
    all untrusted display input — a control byte must render defanged."""

    def test_status_lines_strip_hostile_bytes_from_candidate_and_reason(self):
        out = io.StringIO()
        with patch.object(verify_palette, "_verify_one") as fake_one:
            fake_one.side_effect = [
                (True, "ok"),
                (False, "boom\x1b[31m reason"),
            ]
            with contextlib.redirect_stdout(out):
                verify_palette.verify_candidates(
                    [("xai\x1b[0m", "grok-4.3"), ("openrouter", "bad\x1bmodel")]
                )
        captured = out.getvalue()
        self.assertNotIn("\x1b", captured)
        self.assertIn("xai", captured)  # surrounding text still renders
        self.assertIn("grok-4.3", captured)


class TestVerifyCandidatesOutputSuppression(unittest.TestCase):
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

        with mock.patch.object(verify_palette, "_agent") as fake_agent:
            fake_agent.return_value.chat.side_effect = noisy_ok
            out = _io.StringIO()
            with ctx.redirect_stdout(out):
                records = verify_palette.verify_candidates([("xai", "grok-4.3")])

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

        with mock.patch.object(verify_palette, "_agent") as fake_agent:
            fake_agent.return_value.chat.side_effect = noisy_none
            with ctx.redirect_stdout(_io.StringIO()):
                records = verify_palette.verify_candidates([("copilot", "claude-opus-4.5")])

        self.assertFalse(records[0].ok)
        self.assertIn("not supported", records[0].detail.lower())


# ---------------------------------------------------------------------------
# New tests: default verify cap (2/provider) + --all (U4, #61 zero-touch
# discovery — docs/plans/2026-07-06-001-feat-palette-auto-discovery-plan.md)
# ---------------------------------------------------------------------------


class TestCapCandidatesPure(unittest.TestCase):
    """_cap_candidates is a pure, order-preserving per-provider cap (KTD4).
    No I/O, no printing, no live calls — a plain list-in list-out function."""

    def test_keeps_first_two_per_provider_in_file_order(self):
        pairs = [
            ("openrouter", "model-a"),
            ("openrouter", "model-b"),
            ("openrouter", "model-c"),
            ("xai", "grok-4.3"),
        ]
        capped = verify_palette._cap_candidates(pairs, per_provider=2)
        self.assertEqual(
            capped,
            [
                ("openrouter", "model-a"),
                ("openrouter", "model-b"),
                ("xai", "grok-4.3"),
            ],
        )

    def test_provider_with_one_pair_is_unaffected(self):
        pairs = [("xai", "grok-4.3")]
        self.assertEqual(verify_palette._cap_candidates(pairs, per_provider=2), pairs)

    def test_default_per_provider_is_two(self):
        """Calling without per_provider uses the documented default of 2."""
        pairs = [("xai", "a"), ("xai", "b"), ("xai", "c")]
        self.assertEqual(verify_palette._cap_candidates(pairs), [("xai", "a"), ("xai", "b")])

    def test_empty_list_returns_empty_list(self):
        self.assertEqual(verify_palette._cap_candidates([]), [])

    def test_seven_providers_ten_each_caps_to_fourteen_in_file_order(self):
        """AE3: ~70 pairs across 7 providers caps to 14 (2 each), file order preserved."""
        pairs = [(f"provider{p}", f"model{m}") for p in range(7) for m in range(10)]
        capped = verify_palette._cap_candidates(pairs, per_provider=2)
        self.assertEqual(len(capped), 14)
        for p in range(7):
            provider = f"provider{p}"
            kept = [pair for pair in capped if pair[0] == provider]
            self.assertEqual(kept, [(provider, "model0"), (provider, "model1")])


class TestVerifyPaletteMainCapsByDefault(unittest.TestCase):
    """main() caps candidates to 2/provider by default (R5); --all bypasses the
    cap (R6 — the first paid call still stays inside verify-palette either
    way). The X-of-Y banner prints BEFORE the first verify_candidates() call."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.candidates_path = self.tmp / "palette-candidates.yaml"
        self.palette_path = self.tmp / "palette.yaml"

    def _write_candidates(self, pairs, toolsets=None):
        # Built via yaml.safe_dump (not hand-rolled string interpolation) so a
        # control character embedded in a candidate string round-trips through
        # valid YAML rather than corrupting hand-written syntax.
        data = {
            "candidates": [{"provider": p, "model": m} for p, m in pairs],
            "toolsets": toolsets if toolsets is not None else ["web"],
        }
        self.candidates_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    def _seventy_pairs(self):
        return [(f"provider{p}", f"model{m}") for p in range(7) for m in range(10)]

    def _run_main(self, *, all_candidates=False):
        """Run main() with verify_candidates faked (no live calls); returns
        (exit_code, captured_stdout, [args each verify_candidates call received])."""
        calls = []

        def fake_verify(candidates):
            calls.append(list(candidates))
            print("VERIFY_CALL_MARKER")  # a stdout marker to order-check against the banner
            return [
                verify_palette.VerifyRecord(provider=p, model=m, ok=True) for p, m in candidates
            ]

        out = io.StringIO()
        with patch.object(verify_palette, "_DEFAULT_CANDIDATES_PATH", self.candidates_path), \
             patch.object(verify_palette, "_DEFAULT_PALETTE_PATH", self.palette_path), \
             patch.object(verify_palette, "verify_candidates", side_effect=fake_verify), \
             contextlib.redirect_stdout(out):
            code = verify_palette.main(all_candidates=all_candidates)
        return code, out.getvalue(), calls

    def test_seventy_pairs_capped_to_fourteen_by_default(self):
        self._write_candidates(self._seventy_pairs())
        code, _captured, calls = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1, "verify_candidates must be called exactly once")
        self.assertEqual(len(calls[0]), 14)

    def test_banner_states_x_of_y_before_any_verify_call(self):
        self._write_candidates(self._seventy_pairs())
        _code, captured, _calls = self._run_main()
        banner_idx = captured.find("14 of 70")
        marker_idx = captured.find("VERIFY_CALL_MARKER")
        self.assertNotEqual(banner_idx, -1, captured)
        self.assertNotEqual(marker_idx, -1, captured)
        self.assertLess(banner_idx, marker_idx, "the X-of-Y banner must print before any paid call")

    def test_banner_mentions_all_flag_to_widen(self):
        self._write_candidates(self._seventy_pairs())
        _code, captured, _calls = self._run_main()
        self.assertIn("--all", captured)
        self.assertIn("70", captured)

    def test_all_flag_verifies_every_discovered_pair(self):
        self._write_candidates(self._seventy_pairs())
        code, captured, calls = self._run_main(all_candidates=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls[0]), 70)
        self.assertIn("all 70", captured)

    def test_all_flag_banner_does_not_mention_cap_or_widen_hint(self):
        """Nothing was trimmed when --all is used — the banner must not
        misleadingly reference the per-provider cap or a --all widen hint."""
        self._write_candidates(self._seventy_pairs())
        _code, captured, _calls = self._run_main(all_candidates=True)
        self.assertNotIn("--all to verify", captured)

    def test_providers_under_cap_verified_without_cap_message(self):
        """3 pairs total, none over the per-provider cap -> nothing trimmed;
        the banner must state 'all N', not a misleading X-of-Y cap message."""
        self._write_candidates([("xai", "grok-4.3"), ("openrouter", "a"), ("openrouter", "b")])
        code, captured, calls = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls[0]), 3)
        self.assertNotIn("--all", captured, "nothing was trimmed; must not imply a cap happened")
        self.assertIn("all 3", captured)

    def test_zero_candidates_message_unchanged(self):
        """Regression (test scenario 4): the file-absent path is untouched by
        the cap change — same message, same exit code, no verify call at all."""
        missing = self.tmp / "does-not-exist.yaml"
        out = io.StringIO()
        with patch.object(verify_palette, "_DEFAULT_CANDIDATES_PATH", missing), \
             patch.object(verify_palette, "_DEFAULT_PALETTE_PATH", self.palette_path), \
             contextlib.redirect_stdout(out):
            code = verify_palette.main()
        self.assertEqual(code, 1)
        self.assertIn("No candidates found.", out.getvalue())

    def test_existing_palette_pair_beyond_cap_is_reverified_not_pruned(self):
        """Codex No-ship fold: a capped re-verify must never silently prune a
        previously-verified pair that is still in the candidates file — it is
        RE-VERIFIED (exempt from the cap), so it survives on its own merits."""
        self._write_candidates(
            [("xai", "grok-4.3"), ("xai", "grok-4.3-mini"), ("xai", "grok-4.3-extra")]
        )
        # Existing palette carries a pair beyond the capped first 2.
        self.palette_path.write_text(
            yaml.safe_dump(
                {
                    "models": [
                        {"provider": "xai", "model": "grok-4.3"},
                        {"provider": "xai", "model": "grok-4.3-extra"},
                    ],
                    "toolsets": ["web"],
                }
            ),
            encoding="utf-8",
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _code, _out, calls = self._run_main()
        # The cap still applies to NEW candidates (first 2 per provider); the
        # existing-palette pair beyond it rides on always_keep. Exact order:
        # candidates-file order throughout.
        self.assertEqual(
            calls[0],
            [("xai", "grok-4.3"), ("xai", "grok-4.3-mini"), ("xai", "grok-4.3-extra")],
        )
        self.assertNotIn("will DROP", err.getvalue())

    def test_dropped_pairs_warning_lists_pairs_gone_from_candidates(self):
        """Pairs on the existing palette that are NO LONGER in the candidates
        file cannot be re-verified and WILL drop — the pre-spend stderr
        warning must name them, before any verify call."""
        self._write_candidates([("xai", "grok-4.3")])
        self.palette_path.write_text(
            yaml.safe_dump(
                {
                    "models": [
                        {"provider": "xai", "model": "grok-4.3"},
                        {"provider": "openrouter", "model": "gone/model"},
                    ],
                    "toolsets": ["web"],
                }
            ),
            encoding="utf-8",
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _code, out, _calls = self._run_main()
        warning = err.getvalue()
        self.assertIn("will DROP from the palette", warning)
        self.assertIn("openrouter/gone/model", warning)
        # Disclosure fires in the pre-spend window (main also reached verify).
        self.assertIn("VERIFY_CALL_MARKER", out)

    def test_no_dropped_pairs_warning_without_existing_palette(self):
        self._write_candidates([("xai", "grok-4.3")])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._run_main()
        self.assertNotIn("DROP", err.getvalue())

    def test_no_dropped_pairs_warning_when_pass_covers_palette(self):
        self._write_candidates([("xai", "grok-4.3")])
        self.palette_path.write_text(
            yaml.safe_dump(
                {"models": [{"provider": "xai", "model": "grok-4.3"}], "toolsets": []}
            ),
            encoding="utf-8",
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._run_main()
        self.assertNotIn("DROP", err.getvalue())

    def test_malformed_existing_palette_skips_warning_without_crash(self):
        self._write_candidates([("xai", "grok-4.3")])
        self.palette_path.write_text("models: [unterminated\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _out, _calls = self._run_main()
        self.assertEqual(code, 0)
        self.assertNotIn("DROP", err.getvalue())

    def test_dropped_pairs_warning_sanitizes_hostile_strings(self):
        self._write_candidates([("xai", "grok-4.3")])
        self.palette_path.write_text(
            yaml.safe_dump(
                {
                    "models": [{"provider": "xai", "model": "evil\x1b[31mmodel"}],
                    "toolsets": [],
                }
            ),
            encoding="utf-8",
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._run_main()
        warning = err.getvalue()
        self.assertIn("will DROP", warning)
        self.assertNotIn("\x1b", warning)

    def test_capped_banner_sanitizes_hostile_candidate_strings(self):
        """KTD9: discovered candidate strings are untrusted display input — a
        control byte in a kept candidate must render defanged in the banner
        (mirrors the ESC-stripping pattern used across test_render.py /
        test_preflight.py / test_capture.py's sanitize tests)."""
        self._write_candidates(
            [
                ("xai", "grok\x1b[31m-evil"),
                ("xai", "grok-4.3"),
                ("xai", "grok-4.3-extra"),  # 3rd xai pair -> dropped by the cap
            ]
        )
        _code, captured, _calls = self._run_main()
        self.assertNotIn("\x1b", captured)
        self.assertIn("grok", captured)  # the surrounding text still renders


if __name__ == "__main__":
    unittest.main()
