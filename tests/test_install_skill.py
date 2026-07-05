"""Tests for cadre/install_skill.py — ``cadre install-skill`` (R11/R15/R16, U6
of docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md).

Covers AE3 (idempotent re-run; a symlinked target refused; a group/other-
writable or foreign-owned parent refused; a literal ``~``-relative
``--skills-dir`` exercises expanduser-before-realpath) plus the R15 coupling
check on the shipped ``cadre/data/skill/run.py`` and the copy-plus-re-run
fallback (KTD6), forced via ``copy=True`` since there is no portable runtime
condition that makes symlinking itself fail.

All tests use tempfile paths. The real Hermes skills dir is never touched.
"""

import ast
import contextlib
import io
import os
import shutil
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cadre.install_skill import install_skill
from cadre.resources import skill_dir

# ---------------------------------------------------------------------------
# R15 coupling: the shipped run.py imports ONLY cadre.X (+ stdlib) — no
# fleet_engine, no sys.path bootstrap. Static (AST) parse of the actual
# shipped file — not an executed/imported module, so this is independent of
# whatever the by-path module loader in test_cli.py does.
# ---------------------------------------------------------------------------

_STDLIB_TOP_LEVEL = frozenset({"__future__", "argparse", "contextlib", "sys", "time"})


def _top_level_import_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


class TestShippedRunPyImportCoupling(unittest.TestCase):
    """The de-hack (U6): no repo-relative sys.path bootstrap, cadre-only imports."""

    def setUp(self):
        self.source = (skill_dir() / "run.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_sys_path_mutation(self):
        """No sys.path.insert/append — the actual bootstrap hack. (Not a blanket
        substring ban on "sys.path": the de-hack's own explanatory comment
        correctly mentions it in prose, describing the hack's absence.)"""
        self.assertNotIn("sys.path.insert", self.source)
        self.assertNotIn("sys.path.append", self.source)

    def test_no_repo_root_bootstrap_variable(self):
        self.assertNotIn("_REPO_ROOT", self.source)

    def test_no_e402_noqa_markers(self):
        """Moving imports to normal top-of-file position retires the E402
        (import not at top of file) noqa markers the old ordering needed."""
        self.assertNotIn("noqa: E402", self.source)

    def test_no_fleet_engine_import(self):
        imported = _top_level_import_names(self.tree)
        offenders = [n for n in imported if n == "fleet_engine" or n.startswith("fleet_engine.")]
        self.assertEqual(offenders, [], f"found a fleet_engine import: {offenders}")

    def test_all_non_stdlib_imports_are_cadre_dot_x(self):
        """Every non-stdlib top-level import resolves under the cadre.* namespace (R15)."""
        imported = _top_level_import_names(self.tree)
        non_stdlib = [n for n in imported if n not in _STDLIB_TOP_LEVEL]
        self.assertTrue(non_stdlib, "expected at least one cadre import in the shipped runner")
        for name in non_stdlib:
            self.assertTrue(
                name == "cadre" or name.startswith("cadre."),
                f"{name!r} is not a cadre.* import (R15 — the installed cadre package)",
            )


# ---------------------------------------------------------------------------
# install_skill — the atomic symlink swap (default mode)
# ---------------------------------------------------------------------------


class TestInstallSkillSymlink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.skills_dir = str(Path(self.tmp) / "skills")

    def _entry(self, skills_dir=None):
        return Path(skills_dir or self.skills_dir) / "cadre-fleet"

    def test_creates_symlink_to_installed_skill_dir(self):
        code, msg = install_skill(self.skills_dir)
        self.assertEqual(code, 0, msg)
        entry = self._entry()
        self.assertTrue(entry.is_symlink())
        self.assertEqual(os.path.realpath(entry), os.path.realpath(skill_dir()))

    def test_symlink_resolves_to_real_skill_files(self):
        install_skill(self.skills_dir)
        entry = self._entry()
        self.assertTrue((entry / "SKILL.md").is_file())
        self.assertTrue((entry / "run.py").is_file())

    def test_creates_missing_skills_dir(self):
        """Mirrors the old install.sh's `mkdir -p "$DEST"` — the target need
        not pre-exist."""
        self.assertFalse(Path(self.skills_dir).exists())
        code, _ = install_skill(self.skills_dir)
        self.assertEqual(code, 0)
        self.assertTrue(Path(self.skills_dir).is_dir())

    def test_created_skills_dir_is_owner_only(self):
        install_skill(self.skills_dir)
        mode = stat.S_IMODE(Path(self.skills_dir).stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")

    def test_idempotent_rerun_repoints_to_same_target(self):
        code1, _ = install_skill(self.skills_dir)
        code2, _ = install_skill(self.skills_dir)
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        entry = self._entry()
        self.assertTrue(entry.is_symlink())
        self.assertEqual(os.path.realpath(entry), os.path.realpath(skill_dir()))

    def test_idempotent_rerun_leaves_no_temp_files(self):
        install_skill(self.skills_dir)
        install_skill(self.skills_dir)
        leftovers = [p.name for p in Path(self.skills_dir).iterdir() if "tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_no_skills_dir_given_returns_clean_error(self):
        code, msg = install_skill(None, env={})
        self.assertEqual(code, 1)
        self.assertIn("HERMES_SKILLS_DIR", msg)

    def test_env_fallback_used_when_no_arg(self):
        code, _ = install_skill(None, env={"HERMES_SKILLS_DIR": self.skills_dir})
        self.assertEqual(code, 0)
        self.assertTrue(self._entry().is_symlink())

    def test_explicit_arg_beats_env(self):
        other = str(Path(self.tmp) / "other-skills")
        code, _ = install_skill(self.skills_dir, env={"HERMES_SKILLS_DIR": other})
        self.assertEqual(code, 0)
        self.assertTrue(self._entry().is_symlink())
        self.assertFalse(Path(other).exists(), "the env fallback must be ignored when skills_dir is given")

    def test_tilde_skills_dir_expanded_before_realpath(self):
        """A literal ~-relative --skills-dir exercises expanduser before
        realpath (not an absolute tmp path) — docs/solutions/design-patterns/
        expanduser-before-realpath-for-confined-paths.md."""
        home = os.path.realpath(self.tmp)
        with unittest.mock.patch.dict(os.environ, {"HOME": home}):
            code, msg = install_skill("~/hermes-skills")
        self.assertEqual(code, 0, msg)
        entry = Path(home) / "hermes-skills" / "cadre-fleet"
        self.assertTrue(entry.is_symlink())

    def test_symlinked_target_dir_refused(self):
        elsewhere = Path(self.tmp) / "elsewhere"
        elsewhere.mkdir()
        link = Path(self.tmp) / "skills-link"
        link.symlink_to(elsewhere, target_is_directory=True)

        code, msg = install_skill(str(link))

        self.assertEqual(code, 1)
        self.assertIn("symlink", msg)
        self.assertEqual(list(elsewhere.iterdir()), [], "nothing written through the symlink")
        self.assertTrue(link.is_symlink(), "the symlink itself must be untouched, not replaced")

    def test_group_writable_parent_refused(self):
        d = Path(self.tmp) / "loose-group"
        d.mkdir(mode=0o770)
        os.chmod(d, 0o770)  # chmod after mkdir — mkdir's mode arg is umask-affected
        code, msg = install_skill(str(d))
        self.assertEqual(code, 1)
        self.assertIn("unsafe", msg)

    def test_other_writable_parent_refused(self):
        d = Path(self.tmp) / "loose-other"
        d.mkdir(mode=0o707)
        os.chmod(d, 0o707)
        code, msg = install_skill(str(d))
        self.assertEqual(code, 1)
        self.assertIn("unsafe", msg)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only ownership check")
    def test_foreign_owned_parent_refused(self):
        """A dir owned by a different uid is refused even with safe mode bits.

        Mocks os.getuid (rather than chown, which needs root) to simulate a
        foreign owner on a directory this test process actually owns.
        """
        d = Path(self.tmp) / "foreign"
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            code, msg = install_skill(str(d))
        self.assertEqual(code, 1)
        self.assertIn("unsafe", msg)

    def test_preexisting_0o755_dir_accepted(self):
        """A typical pre-existing Hermes skills dir (0o755 — group/other can
        read+execute but not write) must NOT be refused; only WRITABLE
        group/other bits are unsafe."""
        d = Path(self.tmp) / "hermes-skills"
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)
        code, msg = install_skill(str(d))
        self.assertEqual(code, 0, msg)

    def test_overwrite_disclosed_before_replace(self):
        """The 'replacing existing' print happens BEFORE os.replace runs —
        proven by forcing os.replace to raise and asserting the disclosure
        already landed in stderr (the print executes first regardless of
        whether the subsequent replace succeeds)."""
        install_skill(self.skills_dir)  # first install creates the entry
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            with unittest.mock.patch("cadre.install_skill.os.replace", side_effect=OSError("boom")):
                code, msg = install_skill(self.skills_dir)
        self.assertEqual(code, 1)
        self.assertIn("boom", msg)
        self.assertIn("replacing existing", stderr_buf.getvalue())

    def test_no_disclosure_on_first_install(self):
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            install_skill(self.skills_dir)
        self.assertNotIn("replacing existing", stderr_buf.getvalue())

    def test_dangling_symlink_entry_still_disclosed_and_replaced(self):
        """A dangling symlink at the entry name is caught by lexists (which
        .exists() would miss) and disclosed before being swapped out."""
        Path(self.skills_dir).mkdir(mode=0o700)
        entry = self._entry()
        entry.symlink_to(Path(self.tmp) / "does-not-exist")
        self.assertFalse(entry.exists())  # dangling: exists() is False
        self.assertTrue(os.path.lexists(entry))  # lexists still sees it

        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            code, msg = install_skill(self.skills_dir)

        self.assertEqual(code, 0, msg)
        self.assertIn("replacing existing", stderr_buf.getvalue())
        self.assertEqual(os.path.realpath(entry), os.path.realpath(skill_dir()))

    def test_real_directory_at_entry_fails_cleanly_not_crash(self):
        """Switching from a prior copy-fallback install (a real, non-empty
        dir at <skills_dir>/cadre-fleet) back to symlink mode cannot
        atomically replace a directory with a symlink (POSIX rename requires
        the same type on both sides) — must fail closed with a clear
        message, never crash, and never partially mutate the existing dir."""
        Path(self.skills_dir).mkdir(mode=0o700)
        entry = self._entry()
        entry.mkdir()
        (entry / "SKILL.md").write_text("stale copy", encoding="utf-8")

        try:
            code, msg = install_skill(self.skills_dir)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"install_skill raised instead of failing cleanly: {exc!r}")

        self.assertEqual(code, 1)
        self.assertTrue(entry.is_dir())
        self.assertFalse(entry.is_symlink())
        self.assertEqual((entry / "SKILL.md").read_text(encoding="utf-8"), "stale copy")

    def test_missing_skill_data_fails_cleanly_no_dangling_symlink(self):
        """Fix E regression: os.symlink happily links to a target that does
        not exist — a wheel missing cadre/data/skill/ (a mis-built or
        tampered install) must fail closed instead of creating a DANGLING
        cadre-fleet symlink plus a false (0, "installed...") success.

        Patches ``cadre.install_skill.skill_dir`` — the name bound in
        install_skill's OWN namespace by ``from cadre.resources import
        skill_dir`` — since that is the reference ``_install_symlink``
        actually calls; patching ``cadre.resources.skill_dir`` would not
        affect this already-bound name.
        """
        missing_src = Path(self.tmp) / "nonexistent-skill-data"
        with unittest.mock.patch("cadre.install_skill.skill_dir", return_value=missing_src):
            code, msg = install_skill(self.skills_dir)

        self.assertEqual(code, 1)
        self.assertIn("reinstall cadre", msg)
        entry = self._entry()
        self.assertFalse(
            os.path.lexists(entry),
            "no cadre-fleet symlink (dangling or otherwise) must be created",
        )


# ---------------------------------------------------------------------------
# install_skill(copy=True) — the copy-plus-re-run fallback (KTD6)
#
# Forced branch: on any normal POSIX filesystem symlinking just works, so
# there is no runtime condition that naturally selects this path — every
# test here calls install_skill(..., copy=True) directly.
# ---------------------------------------------------------------------------


class TestInstallSkillCopyFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.skills_dir = str(Path(self.tmp) / "skills")
        self.entry = Path(self.skills_dir) / "cadre-fleet"

    def test_copy_materializes_real_files_not_a_symlink(self):
        code, msg = install_skill(self.skills_dir, copy=True)
        self.assertEqual(code, 0, msg)
        self.assertFalse(self.entry.is_symlink())
        self.assertTrue(self.entry.is_dir())
        self.assertTrue((self.entry / "SKILL.md").is_file())
        self.assertTrue((self.entry / "run.py").is_file())

    def test_copy_content_matches_installed_package(self):
        install_skill(self.skills_dir, copy=True)
        expected_skill = (skill_dir() / "SKILL.md").read_text(encoding="utf-8")
        expected_run = (skill_dir() / "run.py").read_text(encoding="utf-8")
        self.assertEqual((self.entry / "SKILL.md").read_text(encoding="utf-8"), expected_skill)
        self.assertEqual((self.entry / "run.py").read_text(encoding="utf-8"), expected_run)

    def test_copy_files_are_owner_only(self):
        install_skill(self.skills_dir, copy=True)
        for name in ("SKILL.md", "run.py"):
            mode = stat.S_IMODE((self.entry / name).stat().st_mode)
            self.assertEqual(mode, 0o600, f"{name}: expected 0o600, got 0o{mode:03o}")

    def test_copy_idempotent_preserves_edited_file(self):
        """A second copy-install does not overwrite a since-edited file — the
        same preserve-existing contract _seed_files gives the starter fleets/
        personas (the accepted R16 cost of this fallback: it will not
        self-refresh on a package upgrade either)."""
        install_skill(self.skills_dir, copy=True)
        (self.entry / "SKILL.md").write_text("OPERATOR EDIT", encoding="utf-8")

        install_skill(self.skills_dir, copy=True)

        self.assertEqual((self.entry / "SKILL.md").read_text(encoding="utf-8"), "OPERATOR EDIT")

    def test_copy_discloses_preserved_existing_file(self):
        install_skill(self.skills_dir, copy=True)
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            install_skill(self.skills_dir, copy=True)
        self.assertIn("exists", stderr_buf.getvalue())
        self.assertIn("preserved", stderr_buf.getvalue())

    def test_copy_symlinked_destination_file_not_followed(self):
        """A symlink planted at a destination filename is neither followed nor
        overwritten (O_EXCL|O_NOFOLLOW — the _seed_files guard) AND the install
        reports incomplete: a preserved symlink is NOT the real copied file, so
        _install_copy's landed-check flags it (is_symlink) rather than following
        it via .exists() and claiming a false success (Codex confirm)."""
        self.entry.mkdir(parents=True, mode=0o700)
        sentinel = Path(self.tmp) / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET", encoding="utf-8")
        (self.entry / "SKILL.md").symlink_to(sentinel)

        code, msg = install_skill(self.skills_dir, copy=True)

        self.assertEqual(code, 1)
        self.assertIn("incomplete", msg)
        self.assertIn("SKILL.md", msg)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OPERATOR SECRET")
        self.assertTrue((self.entry / "SKILL.md").is_symlink())

    def test_copy_missing_source_file_reports_incomplete_not_false_success(self):
        """C6: _seed_files is warn-and-skip / never-raises, so a missing or
        unreadable source file (e.g. a mis-built or tampered wheel omitting
        run.py) would otherwise leave `entry` partial while this still
        reported (0, "installed..."). Force a source dir missing one of the
        two skill files and confirm the copy path reports the incompleteness
        instead of a false success."""
        partial_src = Path(self.tmp) / "partial-skill-src"
        partial_src.mkdir()
        (partial_src / "SKILL.md").write_text("skill doc", encoding="utf-8")
        # run.py deliberately NOT created — the missing file this test forces.

        with unittest.mock.patch("cadre.install_skill.skill_dir", return_value=partial_src):
            code, msg = install_skill(self.skills_dir, copy=True)

        self.assertEqual(code, 1)
        self.assertIn("incomplete", msg)
        self.assertIn("run.py", msg)
        # The file that DID seed successfully is still on disk (never rolled back) —
        # the fix reports the incomplete state, it doesn't undo the partial seed.
        self.assertTrue((self.entry / "SKILL.md").exists())
        self.assertFalse((self.entry / "run.py").exists())

    def test_copy_symlinked_entry_dir_reports_incomplete(self):
        """A pre-planted symlinked `cadre-fleet` entry dir makes _seed_files
        refuse to seed anything at all (its own directory-symlink guard) —
        the copy path must report that as an incomplete install, not a false
        success, and must never write through the symlink."""
        Path(self.skills_dir).mkdir(mode=0o700)
        elsewhere = Path(self.tmp) / "attacker-target"
        elsewhere.mkdir()
        self.entry.symlink_to(elsewhere, target_is_directory=True)

        code, msg = install_skill(self.skills_dir, copy=True)

        self.assertEqual(code, 1)
        self.assertIn("incomplete", msg)
        self.assertEqual(list(elsewhere.iterdir()), [], "nothing written through the symlinked entry")
        self.assertTrue(self.entry.is_symlink(), "the symlink itself must be untouched")


if __name__ == "__main__":
    unittest.main()
