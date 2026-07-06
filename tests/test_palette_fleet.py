"""Tests for cadre/palette_fleet.py — the generated palette smoke-test fleet
(U5, #61).

All tests inject a tempfile.mkdtemp() path and never touch ~/.cadre.

Test-first: these tests define the contract; the implementation must satisfy them.
"""

from __future__ import annotations

import contextlib
import io
import importlib
import os
import re
import shutil
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

import cadre.palette_fleet as palette_fleet
import cadre.verify_palette as verify_palette
from cadre.capture import resolved_hermes_home
from cadre.cli import validate_command
from cadre.config import FleetConfig
from cadre.verify_palette import VerifyRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _records(*pairs: tuple[str, str]) -> list[VerifyRecord]:
    """Build a list of ok=True VerifyRecords from (provider, model) pairs, in order."""
    return [VerifyRecord(provider=p, model=m, ok=True) for p, m in pairs]


SAFE_PAIRS = [
    ("xai", "grok-4.3"),
    ("openrouter", "google/gemini-3-flash"),
]


# ---------------------------------------------------------------------------
# 1: happy path — only verified pairs appear, header present
# ---------------------------------------------------------------------------


class TestWritePaletteFleetHappyPath(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.target = self.tmp / "palette-fleet.yaml"

    def test_only_ok_records_appear_as_lanes(self):
        records = [
            VerifyRecord(provider="xai", model="grok-4.3", ok=True),
            VerifyRecord(provider="openrouter", model="google/gemini-3-flash", ok=True),
            VerifyRecord(provider="copilot", model="claude-opus-4.8", ok=False, detail="denied"),
        ]
        palette_fleet.write_palette_fleet(records, path=self.target)

        self.assertTrue(self.target.exists())
        content = self.target.read_text(encoding="utf-8")
        self.assertNotIn("copilot", content, "an ok=False provider must never appear")

        data = yaml.safe_load(content)
        providers = {s["provider"] for s in data["specialists"]}
        self.assertEqual(providers, {"xai", "openrouter"})

    def test_header_carries_marker_profile_generated_at_do_not_edit(self):
        palette_fleet.write_palette_fleet(_records(*SAFE_PAIRS), path=self.target)
        content = self.target.read_text(encoding="utf-8")

        self.assertIn("GENERATED", content)
        self.assertIn("do not hand-edit", content)
        self.assertIn("profile:", content)
        self.assertIn(resolved_hermes_home(), content)

        match = re.search(r"generated_at:\s*(\S+)", content)
        self.assertIsNotNone(match, "header must carry a generated_at timestamp")
        datetime.fromisoformat(match.group(1))  # raises ValueError if unparseable

    def test_generated_fleet_is_collect_no_tools_no_persona(self):
        palette_fleet.write_palette_fleet(_records(*SAFE_PAIRS), path=self.target)
        data = yaml.safe_load(self.target.read_text(encoding="utf-8"))

        self.assertEqual(data["convergence"], "collect")
        self.assertNotIn("synthesis", data)
        self.assertNotIn("judge", data)
        for spec in data["specialists"]:
            self.assertEqual(spec["toolset"], [])
            self.assertNotIn("persona", spec)
            self.assertTrue(spec["focus"])


# ---------------------------------------------------------------------------
# 2: a second cycle replaces the file in place
# ---------------------------------------------------------------------------


class TestWritePaletteFleetReplacesInPlace(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.target = self.tmp / "palette-fleet.yaml"

    def test_second_cycle_with_different_pairs_replaces_content(self):
        palette_fleet.write_palette_fleet(
            _records(("xai", "grok-4.3"), ("openrouter", "google/gemini-3-flash")),
            path=self.target,
        )
        first = yaml.safe_load(self.target.read_text(encoding="utf-8"))
        self.assertEqual({s["provider"] for s in first["specialists"]}, {"xai", "openrouter"})

        palette_fleet.write_palette_fleet(
            _records(("copilot", "claude-opus-4.8"), ("nous", "some-model")),
            path=self.target,
        )
        second_text = self.target.read_text(encoding="utf-8")
        second = yaml.safe_load(second_text)
        self.assertEqual({s["provider"] for s in second["specialists"]}, {"copilot", "nous"})
        self.assertNotIn("xai", second_text)
        self.assertNotIn("openrouter", second_text)


# ---------------------------------------------------------------------------
# 3: no OTHER file in the fleet-library dir is ever touched
# ---------------------------------------------------------------------------


class TestWritePaletteFleetDoesNotTouchSiblings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fleets_dir = self.tmp / "fleets"
        self.fleets_dir.mkdir()
        self.target = self.fleets_dir / "palette-fleet.yaml"
        (self.fleets_dir / "code-review.yaml").write_text("name: code-review\n", encoding="utf-8")
        (self.fleets_dir / "doc-review.yaml").write_text("name: doc-review\n", encoding="utf-8")

    def _snapshot(self) -> dict[str, tuple[bytes, int]]:
        return {
            p.name: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in self.fleets_dir.iterdir()
            if p != self.target
        }

    def test_siblings_untouched_across_generate_and_regenerate(self):
        before = self._snapshot()
        palette_fleet.write_palette_fleet(_records(*SAFE_PAIRS), path=self.target)
        self.assertEqual(before, self._snapshot(), "siblings must be untouched by the first generate")

        palette_fleet.write_palette_fleet(
            _records(("copilot", "m"), ("nous", "n")), path=self.target
        )
        self.assertEqual(before, self._snapshot(), "siblings must be untouched by a regenerate")

    def test_siblings_untouched_on_skip_path(self):
        before = self._snapshot()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            palette_fleet.write_palette_fleet(_records(("xai", "grok-4.3")), path=self.target)
        self.assertEqual(before, self._snapshot(), "siblings must be untouched when generation is skipped")


# ---------------------------------------------------------------------------
# 4: coupling test — the generated fleet is genuinely runnable
# ---------------------------------------------------------------------------


class TestGeneratedFleetIsRunnable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.target = self.tmp / "palette-fleet.yaml"

    def test_round_trips_through_fleetconfig_and_cli_validate(self):
        records = _records(
            ("xai", "grok-4.3"),
            ("openrouter", "google/gemini-3-flash"),
            ("copilot", "claude-opus-4.8"),
        )
        palette_fleet.write_palette_fleet(records, path=self.target)

        cfg = FleetConfig.load(self.target)  # must not raise ConfigError
        self.assertEqual(cfg.convergence, "collect")
        self.assertEqual(len(cfg.specialists), 3)
        for spec in cfg.specialists:
            self.assertEqual(spec.toolset, [])
            self.assertEqual(spec.persona, "")
            self.assertTrue(spec.focus)
            self.assertEqual(spec.effective_instruction, spec.focus)

        code, out = validate_command(str(self.target))
        self.assertEqual(code, 0, out)
        self.assertIn("OK:", out)


# ---------------------------------------------------------------------------
# 5: one lane per provider, first verified model, capped at 5
# ---------------------------------------------------------------------------


class TestFirstVerifiedPerProviderCapped(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.target = self.tmp / "palette-fleet.yaml"

    def test_second_model_for_same_provider_is_not_chosen(self):
        records = [
            VerifyRecord(provider="xai", model="grok-4.3", ok=True),
            VerifyRecord(provider="xai", model="grok-4.3-mini", ok=True),
            VerifyRecord(provider="openrouter", model="a/b", ok=True),
        ]
        palette_fleet.write_palette_fleet(records, path=self.target)
        data = yaml.safe_load(self.target.read_text(encoding="utf-8"))
        specialists = data["specialists"]
        self.assertEqual(len(specialists), 2)
        xai_lane = next(s for s in specialists if s["provider"] == "xai")
        self.assertEqual(xai_lane["model"], "grok-4.3")

    def test_six_or_more_verified_providers_capped_to_five_in_order(self):
        providers = [f"provider{i}" for i in range(7)]
        records = [VerifyRecord(provider=p, model="m", ok=True) for p in providers]
        palette_fleet.write_palette_fleet(records, path=self.target)
        data = yaml.safe_load(self.target.read_text(encoding="utf-8"))
        specialists = data["specialists"]
        self.assertEqual(len(specialists), 5)
        self.assertEqual([s["provider"] for s in specialists], providers[:5])


class TestFirstVerifiedPerProviderPure(unittest.TestCase):
    """Direct tests of the pure helper (mirrors verify_palette's
    TestCapCandidatesPure — a private pure function tested directly)."""

    def test_skips_not_ok_records(self):
        records = [
            VerifyRecord(provider="xai", model="grok-4.3", ok=False, detail="denied"),
            VerifyRecord(provider="openrouter", model="a/b", ok=True),
        ]
        chosen = palette_fleet._first_verified_per_provider(records)
        self.assertEqual([r.provider for r in chosen], ["openrouter"])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(palette_fleet._first_verified_per_provider([]), [])


# ---------------------------------------------------------------------------
# 6: fewer than 2 verified providers -> skip, stale file untouched + named
# ---------------------------------------------------------------------------


class TestWritePaletteFleetSkipsBelowMinimum(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.target = self.tmp / "palette-fleet.yaml"

    def test_one_verified_provider_skips_and_names_stale_file(self):
        stale_content = "name: stale-fleet\nconvergence: collect\n"
        self.target.write_text(stale_content, encoding="utf-8")
        before_bytes = self.target.read_bytes()

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            palette_fleet.write_palette_fleet(
                [VerifyRecord(provider="xai", model="grok-4.3", ok=True)],
                path=self.target,
            )

        self.assertEqual(self.target.read_bytes(), before_bytes, "stale file must be byte-identical")
        notice = buf.getvalue()
        self.assertIn(str(self.target), notice, "the notice must name the stale file left in place")
        self.assertIn("preflight", notice.lower())

    def test_zero_verified_providers_skips_without_crash_or_stale_mention(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            palette_fleet.write_palette_fleet(
                [VerifyRecord(provider="xai", model="grok-4.3", ok=False, detail="denied")],
                path=self.target,
            )
        self.assertFalse(self.target.exists())
        self.assertNotIn("left in place", buf.getvalue(), "nothing to name when no file exists")

    def test_empty_records_list_skips_without_crash(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            palette_fleet.write_palette_fleet([], path=self.target)
        self.assertFalse(self.target.exists())


# ---------------------------------------------------------------------------
# 7: write posture — 0600 perms, symlink refusal, unsafe-parent refusal
# ---------------------------------------------------------------------------


class TestWritePaletteFleetPermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_file_is_0o600(self):
        target = self.tmp / "palette-fleet.yaml"
        palette_fleet.write_palette_fleet(_records(*SAFE_PAIRS), path=target)
        mode = stat.S_IMODE(target.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_parent_dir_created_if_missing(self):
        nested = self.tmp / "subdir" / "deeper"
        target = nested / "palette-fleet.yaml"
        palette_fleet.write_palette_fleet(_records(*SAFE_PAIRS), path=target)
        self.assertTrue(target.exists())


class TestWritePaletteFleetSymlinkGuard(unittest.TestCase):
    """Mirrors TestWritePaletteSymlinkGuard (test_palette.py) — write_palette_fleet
    must refuse to follow a symlink planted at the destination (O_NOFOLLOW)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_symlinked_destination_not_written_through(self):
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET", encoding="utf-8")
        target = self.tmp / "palette-fleet.yaml"
        target.symlink_to(sentinel)

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            palette_fleet.write_palette_fleet(_records(*SAFE_PAIRS), path=target)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OPERATOR SECRET")
        self.assertTrue(target.is_symlink(), "the symlink itself must be untouched, not replaced")
        self.assertIn("warning", buf.getvalue().lower())


class TestWritePaletteFleetParentSafety(unittest.TestCase):
    """Mirrors TestWritePaletteParentSafety (test_palette.py) — refuses a
    group/other-writable (or foreign-owned) parent directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_group_writable_parent_refused(self):
        d = self.tmp / "loose"
        d.mkdir(mode=0o770)
        os.chmod(d, 0o770)  # chmod after mkdir — mkdir's mode arg is umask-affected
        target = d / "palette-fleet.yaml"

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            palette_fleet.write_palette_fleet(_records(*SAFE_PAIRS), path=target)

        self.assertFalse(target.exists(), "no file should be written into an unsafe parent")
        self.assertIn("warning", buf.getvalue().lower())


# ---------------------------------------------------------------------------
# 8: a fleet-write failure never changes verify_palette.main()'s exit code
# ---------------------------------------------------------------------------


class TestVerifyPaletteMainFleetHookFailureDegrades(unittest.TestCase):
    """U5 (#61): a fleet-write failure after a successful palette write warns
    to stderr but does NOT change verify_palette.main()'s exit code —
    palette.yaml is the primary deliverable of a verify cycle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _write_candidates(self, pairs):
        data = {
            "candidates": [{"provider": p, "model": m} for p, m in pairs],
            "toolsets": ["web"],
        }
        path = self.tmp / "palette-candidates.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_symlinked_fleet_path_does_not_change_exit_code(self):
        candidates_path = self._write_candidates(SAFE_PAIRS)
        # main() derives the fleet path as palette_path.parent / "fleets" /
        # "palette-fleet.yaml" (verify_palette.py) — plant the symlink there,
        # not at a separately-patched constant (there isn't one; deriving from
        # the already-patched palette_path is what keeps this hermetic).
        palette_path = self.tmp / "palette.yaml"
        fleets_dir = self.tmp / "fleets"
        fleets_dir.mkdir()
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET", encoding="utf-8")
        fleet_path = fleets_dir / "palette-fleet.yaml"
        fleet_path.symlink_to(sentinel)

        ok_records = [VerifyRecord(provider=p, model=m, ok=True) for p, m in SAFE_PAIRS]
        out = io.StringIO()
        err = io.StringIO()
        with patch.object(verify_palette, "_DEFAULT_CANDIDATES_PATH", candidates_path), \
             patch.object(verify_palette, "_DEFAULT_PALETTE_PATH", palette_path), \
             patch.object(verify_palette, "verify_candidates", return_value=ok_records), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = verify_palette.main()

        self.assertEqual(code, 0, "palette.yaml write succeeded -> exit 0 regardless of the fleet-write outcome")
        self.assertTrue(palette_path.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OPERATOR SECRET")
        self.assertTrue(fleet_path.is_symlink(), "the symlink itself must be untouched, not replaced")
        self.assertIn("warning", err.getvalue().lower())

    def test_successful_fleet_write_notice_sanitizes_provider_strings(self):
        """KTD9: a hostile provider string in a generated lane must render
        defanged in the success notice (mirrors verify_candidates' own
        per-candidate sanitize pattern in test_palette.py)."""
        hostile_pairs = [("xai\x1b[31m-evil", "grok-4.3"), ("openrouter", "a/b")]
        candidates_path = self._write_candidates(hostile_pairs)
        palette_path = self.tmp / "palette.yaml"

        ok_records = [VerifyRecord(provider=p, model=m, ok=True) for p, m in hostile_pairs]
        err = io.StringIO()
        with patch.object(verify_palette, "_DEFAULT_CANDIDATES_PATH", candidates_path), \
             patch.object(verify_palette, "_DEFAULT_PALETTE_PATH", palette_path), \
             patch.object(verify_palette, "verify_candidates", return_value=ok_records), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = verify_palette.main()

        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", err.getvalue(), "the ESC byte must be stripped from the notice")


# ---------------------------------------------------------------------------
# 9: module import is side-effect free
# ---------------------------------------------------------------------------


class TestModuleImportHasNoSideEffects(unittest.TestCase):
    def test_default_path_constant_is_not_expanded_at_import_time(self):
        """The default path constant must stay an un-expanded ~-relative Path
        at module scope — expansion (and any filesystem touch) happens only
        inside write_palette_fleet() at call time, mirroring verify_palette.py's
        _DEFAULT_PALETTE_PATH posture."""
        self.assertEqual(str(palette_fleet._DEFAULT_FLEET_PATH), "~/.cadre/fleets/palette-fleet.yaml")

    def test_import_creates_no_real_cadre_directory(self):
        """Merely importing (or reloading) the module must never create the
        real ~/.cadre on the host running the test suite — all directory
        creation is deferred to write_palette_fleet()'s explicit, test-injected
        path."""
        real_cadre = Path("~/.cadre").expanduser()
        pre_existed = real_cadre.exists()
        importlib.reload(palette_fleet)
        if not pre_existed:
            self.assertFalse(real_cadre.exists(), "importing/reloading must not create ~/.cadre")


if __name__ == "__main__":
    unittest.main()
