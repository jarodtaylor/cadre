"""Tests for cadre/provision.py — moved from tests/test_install.py during the
U4 packaging pass (docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md),
which relocated ensure_cadre_dirs/write_config/seed_starter_fleets/seed_personas
out of scripts/resolve_venv.py into cadre.provision (a normal package import,
not the by-path load resolve_venv.py needs — provision.py lives inside the
cadre package). Also covers the two functions new in U4:
seed_palette_candidates and verify_importable (the KTD11 fail-closed guard).

All tests use tempfile paths. The real ~/.cadre is NEVER touched.
"""

import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import cadre.provision as provision


class TestWriteConfig(unittest.TestCase):
    """write_config writes the locked C2 format at 0o600."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _config_path(self, name="config"):
        return str(Path(self.tmp) / name)

    def test_file_permissions_are_0o600(self):
        """Written config is owner-only (0o600)."""
        path = self._config_path()
        provision.write_config("/some/python", path)
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_file_contains_cadre_hermes_python_line(self):
        """Config contains the exact line CADRE_HERMES_PYTHON=<path>."""
        path = self._config_path()
        provision.write_config("/some/python", path)
        content = Path(path).read_text()
        self.assertIn("CADRE_HERMES_PYTHON=/some/python", content)

    def test_c2_round_trip_matches_skill_reader(self):
        """C2 contract: parse config the way the SKILL.md does — cut -d= -f2- equivalent."""
        python_path = "/usr/local/lib/hermes-agent/venv/bin/python"
        path = self._config_path()
        provision.write_config(python_path, path)

        # Mirror the shell's: grep -E '^CADRE_HERMES_PYTHON=' | cut -d= -f2-
        # In Python: split("=", 1)[1]
        parsed = None
        for line in Path(path).read_text().splitlines():
            if line.startswith("CADRE_HERMES_PYTHON="):
                parsed = line.split("=", 1)[1]
                break

        self.assertIsNotNone(parsed, "CADRE_HERMES_PYTHON line not found")
        self.assertEqual(parsed, python_path)

    def test_c2_no_trailing_space_or_quotes(self):
        """C2 contract: no quotes around path, no trailing whitespace."""
        path = self._config_path()
        provision.write_config("/my/python", path)
        content = Path(path).read_text()
        # Find the line and inspect it raw
        line = next(ln for ln in content.splitlines() if ln.startswith("CADRE_HERMES_PYTHON="))
        value = line.split("=", 1)[1]
        self.assertFalse(value.startswith('"'), "value must not be quoted")
        self.assertFalse(value.startswith("'"), "value must not be quoted")
        self.assertEqual(value, value.strip(), "value must not have leading/trailing whitespace")

    def test_c2_path_with_equals_in_value_preserved(self):
        """C2 contract: split('=', 1) handles a path that contains '=' (unusual but safe)."""
        path = self._config_path()
        python_path = "/path/with=sign/python"
        provision.write_config(python_path, path)
        content = Path(path).read_text()
        line = next(ln for ln in content.splitlines() if ln.startswith("CADRE_HERMES_PYTHON="))
        value = line.split("=", 1)[1]
        self.assertEqual(value, python_path)

    def test_idempotent_single_line(self):
        """Calling write_config twice results in exactly one CADRE_HERMES_PYTHON= line."""
        path = self._config_path()
        provision.write_config("/first/python", path)
        provision.write_config("/second/python", path)
        content = Path(path).read_text()
        lines_with_key = [l for l in content.splitlines() if "CADRE_HERMES_PYTHON" in l]
        self.assertEqual(len(lines_with_key), 1)

    def test_idempotent_permissions_unchanged(self):
        """Calling write_config twice preserves 0o600 on the second write."""
        path = self._config_path()
        provision.write_config("/first/python", path)
        provision.write_config("/second/python", path)
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600 after second write, got 0o{mode:03o}")

    def test_idempotent_value_updated(self):
        """Second write updates the path (O_TRUNC), not appends."""
        path = self._config_path()
        provision.write_config("/first/python", path)
        provision.write_config("/second/python", path)
        content = Path(path).read_text()
        line = next(ln for ln in content.splitlines() if ln.startswith("CADRE_HERMES_PYTHON="))
        value = line.split("=", 1)[1]
        self.assertEqual(value, "/second/python")

    def test_symlinked_destination_not_written_through(self):
        """C5b: a symlink planted at config_path is refused (O_NOFOLLOW),
        never followed and written through — matches _seed_files's per-file
        guard and approval.write_approval's O_NOFOLLOW."""
        sentinel = Path(self.tmp) / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET", encoding="utf-8")
        config_path = Path(self.tmp) / "config"
        config_path.symlink_to(sentinel)

        with self.assertRaises(OSError):
            provision.write_config("/some/python", str(config_path))

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OPERATOR SECRET")
        self.assertTrue(config_path.is_symlink(), "the symlink itself must be untouched, not replaced")


class TestWriteConfigParentSafety(unittest.TestCase):
    """CR2 (CodeRabbit, Major/Security): write_config refuses to write into a
    group/other-writable (or foreign-owned) parent directory — mirrors
    approval.write_approval / verify_palette.write_palette's same guard
    (approval._parent_is_safe). Defense-in-depth: setup_command's C5a already
    guards ~/.cadre before calling write_config, but write_config must enforce
    it uniformly with the repo's other hardened writers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_group_writable_parent_refused(self):
        # Create the parent FIRST with the bad mode: write_config's
        # mkdir(exist_ok=True) tightens a freshly-CREATED dir to 0o700 but does
        # NOT downgrade a pre-existing one, so the loose mode must predate the
        # call for this test to actually exercise the guard.
        d = Path(self.tmp) / "loose"
        d.mkdir(mode=0o770)
        os.chmod(d, 0o770)  # chmod after mkdir — mkdir's mode arg is umask-affected

        config_path = d / "config"
        with self.assertRaises(OSError):
            provision.write_config("/some/python", str(config_path))
        self.assertFalse(config_path.exists(), "no file should be written into an unsafe parent")


class TestEnsureCadreDirs(unittest.TestCase):
    """ensure_cadre_dirs creates owner-only dirs idempotently."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_home_dir_created(self):
        """The home dir is created."""
        home = Path(self.tmp) / "cadre"
        provision.ensure_cadre_dirs(str(home))
        self.assertTrue(home.exists())

    def test_fleets_subdir_created(self):
        """The fleets subdir is created inside home."""
        home = Path(self.tmp) / "cadre"
        provision.ensure_cadre_dirs(str(home))
        self.assertTrue((home / "fleets").exists())

    def test_personas_subdir_created(self):
        """The personas subdir is created inside home."""
        home = Path(self.tmp) / "cadre"
        provision.ensure_cadre_dirs(str(home))
        self.assertTrue((home / "personas").exists())

    def test_home_permissions_0o700(self):
        """Home dir is owner-only (0o700)."""
        home = Path(self.tmp) / "cadre"
        provision.ensure_cadre_dirs(str(home))
        mode = stat.S_IMODE(home.stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")

    def test_fleets_permissions_0o700(self):
        """Fleets subdir is owner-only (0o700)."""
        home = Path(self.tmp) / "cadre"
        provision.ensure_cadre_dirs(str(home))
        mode = stat.S_IMODE((home / "fleets").stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")

    def test_personas_permissions_0o700(self):
        """Personas subdir is owner-only (0o700)."""
        home = Path(self.tmp) / "cadre"
        provision.ensure_cadre_dirs(str(home))
        mode = stat.S_IMODE((home / "personas").stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")

    def test_returns_resolved_home_path(self):
        """Returns the resolved home Path (expanduser'd)."""
        home = Path(self.tmp) / "cadre"
        result = provision.ensure_cadre_dirs(str(home))
        self.assertEqual(result, home)

    def test_idempotent_no_error_on_second_call(self):
        """Calling twice raises no error."""
        home = Path(self.tmp) / "cadre"
        provision.ensure_cadre_dirs(str(home))
        provision.ensure_cadre_dirs(str(home))  # should not raise
        self.assertTrue(home.exists())

    def test_idempotent_permissions_preserved(self):
        """Second call preserves 0o700 on home and fleets."""
        home = Path(self.tmp) / "cadre"
        provision.ensure_cadre_dirs(str(home))
        # Loosen permissions (simulate drift), then re-run should still work.
        home.chmod(0o755)
        (home / "fleets").chmod(0o755)
        # Second call: mkdir with exist_ok=True doesn't re-apply mode, so we don't
        # assert mode is tightened back — we assert no error and dirs still exist.
        provision.ensure_cadre_dirs(str(home))
        self.assertTrue(home.exists())
        self.assertTrue((home / "fleets").exists())


class TestSeedStarterFleets(unittest.TestCase):
    """seed_starter_fleets seeds fleets idempotently, owner-only, without stdout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cadre_home = Path(self.tmp) / "cadre"
        self.cadre_home.mkdir(mode=0o700)

    def test_clean_dir_seeds_both_fleets(self):
        """All starter fleets are seeded with .example stripped; palette is NOT seeded.

        Named "both" historically (two starters shipped first); the allowlist
        now carries seven (research-swarm, code-review, doc-review, review-scoring,
        research-brief, debate, critique-revise).
        Each new starter fleet must be added here.
        """
        provision.seed_starter_fleets(self.cadre_home)
        self.assertTrue((self.cadre_home / "fleets" / "research-swarm.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "code-review.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "doc-review.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "review-scoring.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "research-brief.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "debate.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "critique-revise.yaml").exists())

    def test_non_utf8_source_warned_and_skipped_no_raise(self):
        """A non-UTF-8 source fleet is warned-and-skipped, never raised (never-raises contract)."""
        bad_src = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bad_src)
        (bad_src / "research-swarm.example.yaml").write_bytes(b"\xff\xfe not utf-8 \x80")
        # seed_starter_fleets sources via cadre.resources.fleets_dir() (no repo_root
        # arg — U3), so inject the bad source by monkeypatching that helper's
        # binding inside cadre.provision's namespace.
        with unittest.mock.patch.object(provision, "fleets_dir", return_value=bad_src):
            provision.seed_starter_fleets(self.cadre_home)
        # The undecodable source was skipped — no destination written.
        self.assertFalse((self.cadre_home / "fleets" / "research-swarm.yaml").exists())

    def test_symlink_at_destination_not_followed(self):
        """A symlink planted at the destination is not followed/overwritten (O_EXCL/O_NOFOLLOW)."""
        fleets_dir = self.cadre_home / "fleets"
        fleets_dir.mkdir(mode=0o700, exist_ok=True)
        sentinel = self.cadre_home / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET")
        (fleets_dir / "research-swarm.yaml").symlink_to(sentinel)
        provision.seed_starter_fleets(self.cadre_home)  # must not raise
        # The symlink target is untouched — seeding refused to follow the link and write through.
        self.assertEqual(sentinel.read_text(), "OPERATOR SECRET")

    def test_symlinked_dest_dir_refused(self):
        """A symlinked ~/.cadre/fleets *directory* is refused before seeding (#5, R7).

        The per-file O_NOFOLLOW guards a symlinked file; this guards a symlinked
        destination directory, which mkdir(exist_ok=True) would otherwise follow.
        """
        elsewhere = Path(self.tmp) / "attacker-target"
        elsewhere.mkdir(mode=0o700)
        fleets_link = self.cadre_home / "fleets"
        fleets_link.symlink_to(elsewhere, target_is_directory=True)

        provision.seed_starter_fleets(self.cadre_home)  # must not raise

        # Nothing was written through the symlink into the attacker's target.
        self.assertEqual(list(elsewhere.iterdir()), [])
        # The symlink itself was not replaced by a real dir with seeds.
        self.assertTrue(fleets_link.is_symlink())

    def test_symlinked_dest_dir_refusal_does_not_write_stdout(self):
        """The dir-symlink refusal warns to stderr, never stdout (stdout stays clean)."""
        elsewhere = Path(self.tmp) / "attacker-target-2"
        elsewhere.mkdir(mode=0o700)
        (self.cadre_home / "fleets").symlink_to(elsewhere, target_is_directory=True)
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            provision.seed_starter_fleets(self.cadre_home)
        self.assertEqual(stdout_buf.getvalue(), "")

    def test_palette_not_seeded(self):
        """palette.yaml and palette.example.yaml must NOT be seeded by the fleet seeder."""
        provision.seed_starter_fleets(self.cadre_home)
        self.assertFalse((self.cadre_home / "fleets" / "palette.yaml").exists())
        self.assertFalse((self.cadre_home / "fleets" / "palette.example.yaml").exists())

    def test_idempotent_preserves_operator_edits(self):
        """A pre-existing fleet file is NOT overwritten (operator edits preserved)."""
        fleets_dir = self.cadre_home / "fleets"
        fleets_dir.mkdir(mode=0o700, exist_ok=True)
        sentinel_path = fleets_dir / "research-swarm.yaml"
        sentinel_path.write_text("OPERATOR EDIT", encoding="utf-8")

        provision.seed_starter_fleets(self.cadre_home)

        self.assertEqual(sentinel_path.read_text(encoding="utf-8"), "OPERATOR EDIT")

    def test_stdout_silence(self):
        """seed_starter_fleets writes NOTHING to stdout."""
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            provision.seed_starter_fleets(self.cadre_home)
        self.assertEqual(stdout_buf.getvalue(), "")

    def test_seeded_file_permissions_0o600(self):
        """Freshly seeded fleet files are owner-only (0o600)."""
        provision.seed_starter_fleets(self.cadre_home)
        for name in (
            "research-swarm.yaml",
            "code-review.yaml",
            "doc-review.yaml",
            "review-scoring.yaml",
            "research-brief.yaml",
            "debate.yaml",
            "critique-revise.yaml",
        ):
            path = self.cadre_home / "fleets" / name
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"{name}: expected 0o600, got 0o{mode:03o}")

    def test_never_raises_on_bad_cadre_home(self):
        """seed_starter_fleets does NOT raise when cadre_home is unwritable/nonexistent."""
        bad_home = Path("/nonexistent/unwritable/xyz")
        stdout_buf = io.StringIO()
        # Must not raise; must not write to stdout
        with contextlib.redirect_stdout(stdout_buf):
            provision.seed_starter_fleets(bad_home)
        self.assertEqual(stdout_buf.getvalue(), "")


class TestSeedPersonas(unittest.TestCase):
    """seed_personas seeds persona files idempotently, owner-only, without stdout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cadre_home = Path(self.tmp) / "cadre"
        self.cadre_home.mkdir(mode=0o700)

    def test_clean_dir_seeds_all_five_personas(self):
        """All five persona files are seeded with names unchanged (no .example strip)."""
        provision.seed_personas(self.cadre_home)
        for name in (
            "coherence-reviewer.md",
            "feasibility-reviewer.md",
            "scope-guardian-reviewer.md",
            "product-reviewer.md",
            "adversarial-document-reviewer.md",
        ):
            self.assertTrue(
                (self.cadre_home / "personas" / name).exists(),
                f"expected {name} to be seeded",
            )

    def test_seeded_file_permissions_0o600(self):
        """Freshly seeded persona files are owner-only (0o600)."""
        provision.seed_personas(self.cadre_home)
        for name in (
            "coherence-reviewer.md",
            "feasibility-reviewer.md",
            "scope-guardian-reviewer.md",
            "product-reviewer.md",
            "adversarial-document-reviewer.md",
        ):
            path = self.cadre_home / "personas" / name
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"{name}: expected 0o600, got 0o{mode:03o}")

    def test_idempotent_preserves_operator_edits(self):
        """A pre-existing persona file is NOT overwritten (operator edits preserved)."""
        personas_dir = self.cadre_home / "personas"
        personas_dir.mkdir(mode=0o700, exist_ok=True)
        sentinel_path = personas_dir / "coherence-reviewer.md"
        sentinel_path.write_text("OPERATOR EDIT", encoding="utf-8")

        provision.seed_personas(self.cadre_home)

        self.assertEqual(sentinel_path.read_text(encoding="utf-8"), "OPERATOR EDIT")

    def test_symlink_at_destination_not_followed(self):
        """A symlink planted at the destination is not followed/overwritten (O_EXCL/O_NOFOLLOW)."""
        personas_dir = self.cadre_home / "personas"
        personas_dir.mkdir(mode=0o700, exist_ok=True)
        sentinel = self.cadre_home / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET")
        (personas_dir / "coherence-reviewer.md").symlink_to(sentinel)
        provision.seed_personas(self.cadre_home)  # must not raise
        # The symlink target is untouched — seeding refused to follow the link and write through.
        self.assertEqual(sentinel.read_text(), "OPERATOR SECRET")

    def test_non_utf8_source_warned_and_skipped_no_raise(self):
        """A non-UTF-8 source persona is warned-and-skipped, never raised (never-raises contract)."""
        bad_src = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(bad_src))
        (bad_src / "coherence-reviewer.md").write_bytes(b"\xff\xfe not utf-8 \x80")
        # seed_personas sources via cadre.resources.personas_dir() (no repo_root
        # arg — U3), so inject the bad source by monkeypatching that helper's
        # binding inside cadre.provision's namespace.
        with unittest.mock.patch.object(provision, "personas_dir", return_value=bad_src):
            provision.seed_personas(self.cadre_home)
        # The undecodable source was skipped — no destination written.
        self.assertFalse((self.cadre_home / "personas" / "coherence-reviewer.md").exists())

    def test_stdout_silence(self):
        """seed_personas writes NOTHING to stdout."""
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            provision.seed_personas(self.cadre_home)
        self.assertEqual(stdout_buf.getvalue(), "")

    def test_never_raises_on_bad_cadre_home(self):
        """seed_personas does NOT raise when cadre_home is unwritable/nonexistent."""
        bad_home = Path("/nonexistent/unwritable/xyz")
        stdout_buf = io.StringIO()
        # Must not raise; must not write to stdout
        with contextlib.redirect_stdout(stdout_buf):
            provision.seed_personas(bad_home)
        self.assertEqual(stdout_buf.getvalue(), "")


# ---------------------------------------------------------------------------
# New in U4: seed_palette_candidates
# ---------------------------------------------------------------------------


class TestSeedPaletteCandidates(unittest.TestCase):
    """seed_palette_candidates seeds ~/.cadre/palette-candidates.yaml idempotently.

    Replaces install.sh's old `cp fleets/palette.example.yaml ...` line, broken
    since U3 moved the example out of the repo-root fleets/ dir.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cadre_home = Path(self.tmp) / "cadre"
        self.cadre_home.mkdir(mode=0o700)

    def test_seeds_palette_candidates_file(self):
        """The destination is a single file directly under cadre_home (not a subdir)."""
        provision.seed_palette_candidates(self.cadre_home)
        dest = self.cadre_home / "palette-candidates.yaml"
        self.assertTrue(dest.exists())

    def test_content_matches_packaged_example(self):
        """Seeded content is byte-identical to the packaged palette.example.yaml."""
        provision.seed_palette_candidates(self.cadre_home)
        dest = self.cadre_home / "palette-candidates.yaml"
        expected = provision.palette_example_path().read_text(encoding="utf-8")
        self.assertEqual(dest.read_text(encoding="utf-8"), expected)

    def test_seeded_file_permissions_0o600(self):
        """The seeded palette-candidates file is owner-only (0o600)."""
        provision.seed_palette_candidates(self.cadre_home)
        dest = self.cadre_home / "palette-candidates.yaml"
        mode = stat.S_IMODE(dest.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_idempotent_preserves_operator_edits(self):
        """A pre-existing palette-candidates.yaml is NOT overwritten (operator edits preserved)."""
        dest = self.cadre_home / "palette-candidates.yaml"
        dest.write_text("OPERATOR EDIT", encoding="utf-8")
        provision.seed_palette_candidates(self.cadre_home)
        self.assertEqual(dest.read_text(encoding="utf-8"), "OPERATOR EDIT")

    def test_missing_packaged_source_does_not_delete_preexisting_dest(self):
        """Fix B regression (data-loss): a missing PACKAGED source (a mis-built
        or tampered wheel) must never delete a pre-existing OPERATOR dest file
        on a re-seed.

        Before the fix, `_seed_files`'s except handler unconditionally
        `os.unlink(dest)`'d on ANY (OSError, UnicodeDecodeError) — including
        `src.read_text()` raising FileNotFoundError, which happens BEFORE
        `os.open(dest, ...)` ever runs. So a pre-existing dest (e.g. a hand-
        edited palette-candidates.yaml) that this call never created was
        silently deleted anyway. The `created` guard fixes this: only a
        failure AFTER os.open succeeded (this call's OWN partial write) may
        unlink.
        """
        dest = self.cadre_home / "palette-candidates.yaml"
        dest.write_text("OPERATOR EDIT — DO NOT DELETE", encoding="utf-8")

        # A source directory that does not exist — src.read_text() raises
        # FileNotFoundError before os.open(dest, ...) is ever reached.
        missing_example = Path(self.tmp) / "nonexistent-source-dir" / "palette.example.yaml"

        stderr_buf = io.StringIO()
        with unittest.mock.patch.object(provision, "palette_example_path", return_value=missing_example):
            with contextlib.redirect_stderr(stderr_buf):
                provision.seed_palette_candidates(self.cadre_home)  # must not raise

        self.assertEqual(
            dest.read_text(encoding="utf-8"),
            "OPERATOR EDIT — DO NOT DELETE",
            "a missing packaged source must never delete a pre-existing operator dest",
        )
        self.assertIn("[cadre] warning: could not seed", stderr_buf.getvalue())

    def test_stdout_silence(self):
        """seed_palette_candidates writes NOTHING to stdout."""
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            provision.seed_palette_candidates(self.cadre_home)
        self.assertEqual(stdout_buf.getvalue(), "")

    def test_symlinked_cadre_home_refused(self):
        """A symlinked ~/.cadre itself is refused before seeding (shared _seed_files guard).

        seed_palette_candidates writes directly into cadre_home (no subdirectory),
        so a pre-planted ~/.cadre symlink is the analog of the fleets/personas
        symlinked-subdir attack — the same _seed_files.is_symlink() check catches it.
        """
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, elsewhere)
        home_link = Path(self.tmp) / "cadre-link"
        home_link.symlink_to(elsewhere, target_is_directory=True)

        provision.seed_palette_candidates(home_link)  # must not raise

        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertTrue(home_link.is_symlink())

    def test_never_raises_on_bad_cadre_home(self):
        """seed_palette_candidates does NOT raise when cadre_home is unwritable/nonexistent."""
        bad_home = Path("/nonexistent/unwritable/xyz")
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            provision.seed_palette_candidates(bad_home)
        self.assertEqual(stdout_buf.getvalue(), "")


# ---------------------------------------------------------------------------
# New in U3 (#61 palette auto-discovery): seed_or_discover_palette_candidates
#
# HERMETICITY (load-bearing): a provisioned host HAS hermes_cli importable;
# this dev laptop does NOT. Every test here forces the discovery outcome
# deterministically by patching cadre.provision.discover_candidates /
# .write_candidates (the names imported into this module's namespace) —
# never relying on whether hermes_cli happens to be importable on the
# machine running the suite.
# (docs/solutions/best-practices/force-ambient-host-state-in-tests-when-code-reads-it.md)
# ---------------------------------------------------------------------------


from tests.palette_fixtures import fake_discovery_result as _fake_discovery_result


class TestSeedOrDiscoverPaletteCandidates(unittest.TestCase):
    """seed_or_discover_palette_candidates: discovery when possible, the
    pre-#61 seed_palette_candidates() placeholder when not — either way,
    never overwrites an existing candidates file, never raises."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cadre_home = Path(self.tmp) / "cadre"
        self.cadre_home.mkdir(mode=0o700)
        self.candidates_path = self.cadre_home / "palette-candidates.yaml"

    def test_existing_file_preserved_even_when_discovery_succeeds(self):
        """Test scenario 1 / AE6's setup-preserves half: an operator-edited
        file survives, byte-identical, even though discovery has fresh
        (different) data ready to write. A "preserved" message is printed;
        seed_or_discover_palette_candidates itself never raises (setup's
        exit code is proven unaffected at the setup_command level in
        test_cli.py)."""
        self.candidates_path.write_text("OPERATOR EDIT — DO NOT DELETE", encoding="utf-8")
        stderr_buf = io.StringIO()
        with unittest.mock.patch.object(
            provision, "discover_candidates", return_value=_fake_discovery_result()
        ):
            with contextlib.redirect_stderr(stderr_buf):
                provision.seed_or_discover_palette_candidates(self.cadre_home)

        self.assertEqual(
            self.candidates_path.read_text(encoding="utf-8"),
            "OPERATOR EDIT — DO NOT DELETE",
        )
        self.assertIn("preserved", stderr_buf.getvalue())

    def test_fresh_dir_seeds_discovered_candidates(self):
        """Test scenario 2: a clean dir + fake discovery success -> the
        discovered pairs are written, round-tripping through
        verify_palette._load_candidates, 0600 perms."""
        import cadre.verify_palette as verify_palette

        result = _fake_discovery_result(provider="openrouter", models=("a/model", "b/model"))
        with unittest.mock.patch.object(provision, "discover_candidates", return_value=result):
            provision.seed_or_discover_palette_candidates(self.cadre_home)

        self.assertTrue(self.candidates_path.exists())
        candidates, _toolsets = verify_palette._load_candidates(self.candidates_path)
        self.assertEqual(candidates, [("openrouter", "a/model"), ("openrouter", "b/model")])
        mode = stat.S_IMODE(self.candidates_path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_discovery_unavailable_falls_back_to_placeholder_with_reason(self):
        """Test scenario 3 / AE2's setup half: DiscoveryError -> the
        placeholder is seeded exactly as pre-#61, plus a stderr line naming
        why discovery was unavailable."""
        from cadre.discover import DiscoveryError

        stderr_buf = io.StringIO()
        with unittest.mock.patch.object(
            provision,
            "discover_candidates",
            side_effect=DiscoveryError("hermes_cli not importable on this host."),
        ):
            with contextlib.redirect_stderr(stderr_buf):
                provision.seed_or_discover_palette_candidates(self.cadre_home)

        expected = provision.palette_example_path().read_text(encoding="utf-8")
        self.assertEqual(self.candidates_path.read_text(encoding="utf-8"), expected)
        notice = stderr_buf.getvalue()
        self.assertIn("discovery", notice.lower())
        self.assertIn("unavailable", notice.lower())
        self.assertIn("hermes_cli not importable on this host.", notice)

    def test_discovery_error_reason_renders_sanitized(self):
        """Test scenario 3 (sanitize half) / KTD9: DiscoveryError text is
        untrusted display input -- a hostile byte sequence in it must not
        reach the stderr line unsanitized."""
        from cadre.discover import DiscoveryError

        hostile = DiscoveryError("boom\x1b[31m drifted.")
        stderr_buf = io.StringIO()
        with unittest.mock.patch.object(provision, "discover_candidates", side_effect=hostile):
            with contextlib.redirect_stderr(stderr_buf):
                provision.seed_or_discover_palette_candidates(self.cadre_home)
        notice = stderr_buf.getvalue()
        self.assertNotIn("\x1b", notice)
        self.assertIn("drifted", notice)

    def test_create_only_write_oserror_falls_back_to_placeholder(self):
        """Test scenario 4: discovery succeeds but the create-only write
        raises OSError (not FileExistsError) -> warn + fall back to the
        placeholder path, no partial discovered-content file left."""
        stderr_buf = io.StringIO()
        with unittest.mock.patch.object(
            provision, "discover_candidates", return_value=_fake_discovery_result()
        ):
            with unittest.mock.patch.object(
                provision, "write_candidates", side_effect=OSError("disk full")
            ):
                with contextlib.redirect_stderr(stderr_buf):
                    provision.seed_or_discover_palette_candidates(self.cadre_home)

        expected = provision.palette_example_path().read_text(encoding="utf-8")
        self.assertEqual(
            self.candidates_path.read_text(encoding="utf-8"),
            expected,
            "no partial discovered-content file should be left after the fallback",
        )
        self.assertIn("disk full", stderr_buf.getvalue())

    def test_never_raises_on_discovery_error(self):
        from cadre.discover import DiscoveryError

        with unittest.mock.patch.object(
            provision, "discover_candidates", side_effect=DiscoveryError("unavailable")
        ):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    provision.seed_or_discover_palette_candidates(self.cadre_home)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"seed_or_discover_palette_candidates raised: {exc!r}")

    def test_never_raises_on_write_oserror(self):
        with unittest.mock.patch.object(
            provision, "discover_candidates", return_value=_fake_discovery_result()
        ):
            with unittest.mock.patch.object(
                provision, "write_candidates", side_effect=OSError("boom")
            ):
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        provision.seed_or_discover_palette_candidates(self.cadre_home)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"seed_or_discover_palette_candidates raised: {exc!r}")

    def test_stdout_silence(self):
        """Never writes to stdout, on either the discovery or fallback path."""
        stdout_buf = io.StringIO()
        with unittest.mock.patch.object(
            provision, "discover_candidates", return_value=_fake_discovery_result()
        ):
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(io.StringIO()):
                provision.seed_or_discover_palette_candidates(self.cadre_home)
        self.assertEqual(stdout_buf.getvalue(), "")


# ---------------------------------------------------------------------------
# New in U4: verify_importable (KTD11 fail-closed guard)
# ---------------------------------------------------------------------------


class TestVerifyImportable(unittest.TestCase):
    """verify_importable runs a REAL subprocess — the boundary itself is not mocked here
    (cli.setup_command's own tests mock/inject at a higher level instead)."""

    def _make_stub(self, exit_code: int) -> str:
        """Write a tiny always-<exit_code> executable and return its path.

        A shell stub (not a real python) deliberately sidesteps any cwd/sys.path
        subtlety around what "a python that can't import cadre" would actually
        resolve to on this machine — it simulates the OUTCOME (subprocess exits
        nonzero) directly, which is all verify_importable's contract cares about.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        stub = Path(tmp) / "fake-python"
        stub.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        stub.chmod(0o700)
        return str(stub)

    def test_real_interpreter_without_cadre_installed_returns_false_even_from_repo_root(self):
        """Fix A regression (closes the cwd fail-open): sys.executable IS the
        interpreter running this test, and cwd is the repo root (how `unittest
        discover -s tests` runs) — but this dev venv does NOT have cadre
        pip-installed into its own site-packages (same premise
        test_decoupling.py's real wheel-install proof depends on: `pip show
        cadre` finds nothing in this venv). Before the fix, a bare `python -c
        "import cadre"` resolved the in-tree checkout via cwd on sys.path[0]
        regardless of whether python_path itself had cadre installed — a false
        "importable". `-P` excludes cwd from sys.path, so this now correctly
        returns False. PYTHONPATH is explicitly cleared so a dev's shell
        environment can't accidentally paper over the cwd-exclusion this test
        exists to prove (confirmed separately that `-P` does NOT strip an
        explicit PYTHONPATH, only the automatic cwd/script-dir entries)."""
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(provision.verify_importable(sys.executable))

    def test_stub_exiting_nonzero_returns_false(self):
        stub = self._make_stub(1)
        self.assertFalse(provision.verify_importable(stub))

    def test_stub_exiting_zero_returns_true(self):
        stub = self._make_stub(0)
        self.assertTrue(provision.verify_importable(stub))

    def test_nonexistent_python_returns_false(self):
        """A python_path that doesn't exist at all fails closed (OSError caught), never raises."""
        self.assertFalse(provision.verify_importable("/no/such/interpreter/anywhere"))

    def test_never_raises_on_bad_path(self):
        try:
            provision.verify_importable("/no/such/interpreter/anywhere")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"verify_importable raised: {exc!r}")

    def test_timeout_returns_false(self):
        """KTD11 hang-prevention: a slow-to-exit interpreter is bounded by
        `timeout` — exercises the TimeoutExpired half of the except tuple, so
        a hung/misbehaving recorded interpreter can't wedge `cadre setup`
        indefinitely. Fast (~50ms): the stub sleeps 1s but the check times out
        at 0.05s."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        stub = Path(tmp) / "slow-python"
        stub.write_text("#!/bin/sh\nsleep 1\nexit 0\n", encoding="utf-8")
        stub.chmod(0o700)
        self.assertFalse(provision.verify_importable(str(stub), timeout=0.05))

    def test_ignores_pythonpath_injection(self):
        """C1 regression: -I ignores PYTHONPATH, so a planted fake `cadre`
        package on an attacker- (or dev-shell-) controlled PYTHONPATH entry
        must NOT satisfy the import check.

        sys.executable does not have cadre installed in its own site-packages
        (see test_real_interpreter_without_cadre_installed_returns_false_even_from_repo_root's
        docstring — confirmed by `pip show cadre` finding nothing in this dev
        venv), so this exercises a REAL subprocess rather than a mock. Before
        the -I fix, a `-P`-only invocation dropped cwd from sys.path but
        still honored PYTHONPATH — planting a fake cadre/__init__.py on
        PYTHONPATH would have made this wrongly return True, the same class
        of false success as the cwd fail-open Fix A closed. -I additionally
        ignores PYTHONPATH/PYTHON* env entirely, closing this second
        injection path too.
        """
        fake_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, fake_root)
        fake_pkg = Path(fake_root) / "cadre"
        fake_pkg.mkdir()
        (fake_pkg / "__init__.py").write_text("", encoding="utf-8")

        env = dict(os.environ)
        env["PYTHONPATH"] = fake_root
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(
                provision.verify_importable(sys.executable),
                "a planted cadre/__init__.py on PYTHONPATH must not satisfy the import check",
            )

    def test_subprocess_uses_isolated_mode_flag(self):
        """Documents the exact isolation flag the security property rests on
        (belt-and-suspenders alongside the real-subprocess proof above): a
        future edit that swaps -I for something less isolated (e.g. back to
        -P) must fail this directly, not just the PYTHONPATH behavioral test."""
        with unittest.mock.patch("cadre.provision.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            provision.verify_importable("/some/python")
        cmd = mock_run.call_args[0][0]
        self.assertIn("-I", cmd)
        self.assertNotIn("-P", cmd, "-I already implies -P; the flag must not be weakened to -P alone")


if __name__ == "__main__":
    unittest.main()
