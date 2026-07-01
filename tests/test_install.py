"""Tests for scripts/resolve_venv.py — the testable core of U4 install.

All tests use tempfile paths. The real ~/.cadre is NEVER touched.

Loads the module via importlib (scripts/ is not a Python package),
mirroring the by-path import pattern in test_cli.py:_load_skill_module.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


def _load_resolve_venv():
    """Load scripts/resolve_venv.py as a module via importlib (not a package import)."""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "resolve_venv.py"
    spec = importlib.util.spec_from_file_location("resolve_venv", script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["resolve_venv"] = mod
    spec.loader.exec_module(mod)
    return mod


# Load once; all test classes reference module-level `rv`.
rv = _load_resolve_venv()


class TestResolveVenvOverrideBeatsEnv(unittest.TestCase):
    """Explicit override arg beats env, even when probe paths exist."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_env_override_returned_without_probing(self):
        """CADRE_HERMES_PYTHON set → that path returned; probe paths NOT checked."""
        # Create a real file at probe_paths so we'd accidentally pick it up.
        probe_file = Path(self.tmp) / "probe-python"
        probe_file.touch()

        result = rv.resolve_venv(
            env={"CADRE_HERMES_PYTHON": "/x/py"},
            probe_paths=[str(probe_file), "/nope"],
        )
        self.assertEqual(result, "/x/py")

    def test_explicit_arg_beats_env(self):
        """Explicit override arg takes precedence over CADRE_HERMES_PYTHON env."""
        result = rv.resolve_venv(
            "/arg/py",
            env={"CADRE_HERMES_PYTHON": "/env/py"},
        )
        self.assertEqual(result, "/arg/py")

    def test_override_expanduser(self):
        """An override with ~ is expanduser'd."""
        result = rv.resolve_venv("~/bin/python", env={})
        expected = str(Path("~/bin/python").expanduser())
        self.assertEqual(result, expected)

    def test_env_expanduser(self):
        """A ~ in the env value is expanduser'd."""
        result = rv.resolve_venv(env={"CADRE_HERMES_PYTHON": "~/bin/python"})
        expected = str(Path("~/bin/python").expanduser())
        self.assertEqual(result, expected)

    def test_override_returned_even_if_nonexistent(self):
        """Override is returned verbatim (expanduser'd) even if the path doesn't exist."""
        result = rv.resolve_venv("/this/path/does/not/exist/python", env={})
        self.assertEqual(result, "/this/path/does/not/exist/python")

    def test_env_returned_even_if_nonexistent(self):
        """Env value is returned verbatim (expanduser'd) even if the path doesn't exist."""
        result = rv.resolve_venv(env={"CADRE_HERMES_PYTHON": "/also/nonexistent/python"})
        self.assertEqual(result, "/also/nonexistent/python")


class TestResolveVenvProbing(unittest.TestCase):
    """Probe path precedence and existence checks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_probe_first_path_returned(self):
        """First existing probe path is returned when no override/env set."""
        first = Path(self.tmp) / "python-first"
        second = Path(self.tmp) / "python-second"
        first.touch()
        second.touch()

        result = rv.resolve_venv(
            env={},
            probe_paths=[str(first), str(second)],
        )
        self.assertEqual(result, str(first))

    def test_probe_second_path_when_first_absent(self):
        """Falls through to the second path when the first does not exist."""
        first = Path(self.tmp) / "python-missing"
        second = Path(self.tmp) / "python-present"
        second.touch()
        # first is NOT created

        result = rv.resolve_venv(
            env={},
            probe_paths=[str(first), str(second)],
        )
        self.assertEqual(result, str(second))

    def test_probe_expanduser(self):
        """Probe paths are expanduser'd before existence check."""
        # We can't easily test a real ~ path portably, but we can verify that a
        # non-tilde path works (expanduser on an abs path is a no-op, still works).
        real_file = Path(self.tmp) / "pybin"
        real_file.touch()
        result = rv.resolve_venv(env={}, probe_paths=[str(real_file)])
        self.assertEqual(result, str(real_file))


class TestResolveVenvError(unittest.TestCase):
    """No override and no existing probe path → RuntimeError with informative message."""

    def test_no_match_raises_runtime_error(self):
        """All paths absent and no override → RuntimeError."""
        with self.assertRaises(RuntimeError):
            rv.resolve_venv(
                env={},
                probe_paths=["/nonexistent/path/1", "/nonexistent/path/2"],
            )

    def test_error_message_mentions_cadre_hermes_python(self):
        """RuntimeError message names the CADRE_HERMES_PYTHON override knob."""
        try:
            rv.resolve_venv(env={}, probe_paths=["/nope"])
        except RuntimeError as exc:
            self.assertIn("CADRE_HERMES_PYTHON", str(exc))
        else:
            self.fail("Expected RuntimeError")

    def test_error_message_mentions_venv_python_flag(self):
        """RuntimeError message names the --venv-python flag."""
        try:
            rv.resolve_venv(env={}, probe_paths=["/nope"])
        except RuntimeError as exc:
            self.assertIn("--venv-python", str(exc))
        else:
            self.fail("Expected RuntimeError")


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
        rv.write_config("/some/python", path)
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got 0o{mode:03o}")

    def test_file_contains_cadre_hermes_python_line(self):
        """Config contains the exact line CADRE_HERMES_PYTHON=<path>."""
        path = self._config_path()
        rv.write_config("/some/python", path)
        content = Path(path).read_text()
        self.assertIn("CADRE_HERMES_PYTHON=/some/python", content)

    def test_c2_round_trip_matches_skill_reader(self):
        """C2 contract: parse config the way the SKILL.md does — cut -d= -f2- equivalent."""
        python_path = "/usr/local/lib/hermes-agent/venv/bin/python"
        path = self._config_path()
        rv.write_config(python_path, path)

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
        rv.write_config("/my/python", path)
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
        rv.write_config(python_path, path)
        content = Path(path).read_text()
        line = next(ln for ln in content.splitlines() if ln.startswith("CADRE_HERMES_PYTHON="))
        value = line.split("=", 1)[1]
        self.assertEqual(value, python_path)

    def test_idempotent_single_line(self):
        """Calling write_config twice results in exactly one CADRE_HERMES_PYTHON= line."""
        path = self._config_path()
        rv.write_config("/first/python", path)
        rv.write_config("/second/python", path)
        content = Path(path).read_text()
        lines_with_key = [l for l in content.splitlines() if "CADRE_HERMES_PYTHON" in l]
        self.assertEqual(len(lines_with_key), 1)

    def test_idempotent_permissions_unchanged(self):
        """Calling write_config twice preserves 0o600 on the second write."""
        path = self._config_path()
        rv.write_config("/first/python", path)
        rv.write_config("/second/python", path)
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600 after second write, got 0o{mode:03o}")

    def test_idempotent_value_updated(self):
        """Second write updates the path (O_TRUNC), not appends."""
        path = self._config_path()
        rv.write_config("/first/python", path)
        rv.write_config("/second/python", path)
        content = Path(path).read_text()
        line = next(ln for ln in content.splitlines() if ln.startswith("CADRE_HERMES_PYTHON="))
        value = line.split("=", 1)[1]
        self.assertEqual(value, "/second/python")


class TestEnsureCadreDirs(unittest.TestCase):
    """ensure_cadre_dirs creates owner-only dirs idempotently."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_home_dir_created(self):
        """The home dir is created."""
        home = Path(self.tmp) / "cadre"
        rv.ensure_cadre_dirs(str(home))
        self.assertTrue(home.exists())

    def test_fleets_subdir_created(self):
        """The fleets subdir is created inside home."""
        home = Path(self.tmp) / "cadre"
        rv.ensure_cadre_dirs(str(home))
        self.assertTrue((home / "fleets").exists())

    def test_personas_subdir_created(self):
        """The personas subdir is created inside home."""
        home = Path(self.tmp) / "cadre"
        rv.ensure_cadre_dirs(str(home))
        self.assertTrue((home / "personas").exists())

    def test_home_permissions_0o700(self):
        """Home dir is owner-only (0o700)."""
        home = Path(self.tmp) / "cadre"
        rv.ensure_cadre_dirs(str(home))
        mode = stat.S_IMODE(home.stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")

    def test_fleets_permissions_0o700(self):
        """Fleets subdir is owner-only (0o700)."""
        home = Path(self.tmp) / "cadre"
        rv.ensure_cadre_dirs(str(home))
        mode = stat.S_IMODE((home / "fleets").stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")

    def test_personas_permissions_0o700(self):
        """Personas subdir is owner-only (0o700)."""
        home = Path(self.tmp) / "cadre"
        rv.ensure_cadre_dirs(str(home))
        mode = stat.S_IMODE((home / "personas").stat().st_mode)
        self.assertEqual(mode, 0o700, f"expected 0o700, got 0o{mode:03o}")

    def test_returns_resolved_home_path(self):
        """Returns the resolved home Path (expanduser'd)."""
        home = Path(self.tmp) / "cadre"
        result = rv.ensure_cadre_dirs(str(home))
        self.assertEqual(result, home)

    def test_idempotent_no_error_on_second_call(self):
        """Calling twice raises no error."""
        home = Path(self.tmp) / "cadre"
        rv.ensure_cadre_dirs(str(home))
        rv.ensure_cadre_dirs(str(home))  # should not raise
        self.assertTrue(home.exists())

    def test_idempotent_permissions_preserved(self):
        """Second call preserves 0o700 on home and fleets."""
        home = Path(self.tmp) / "cadre"
        rv.ensure_cadre_dirs(str(home))
        # Loosen permissions (simulate drift), then re-run should still work.
        home.chmod(0o755)
        (home / "fleets").chmod(0o755)
        # Second call: mkdir with exist_ok=True doesn't re-apply mode, so we don't
        # assert mode is tightened back — we assert no error and dirs still exist.
        rv.ensure_cadre_dirs(str(home))
        self.assertTrue(home.exists())
        self.assertTrue((home / "fleets").exists())


# ---------------------------------------------------------------------------
# New test: resolve_venv.main() stdout/stderr contract (FIX 6 / install.sh PYBIN)
# ---------------------------------------------------------------------------


class TestMainStdoutContract(unittest.TestCase):
    """main() prints ONLY the resolved python path to stdout; diagnostics to stderr.

    This locks the `PYBIN="$(python3 scripts/resolve_venv.py "$@")"` contract in
    install.sh: command substitution captures stdout, so any stray text there would
    corrupt PYBIN.
    """

    def test_explicit_venv_python_stdout_is_only_path(self):
        """main(["--venv-python", "/some/python"]) stdout == "/some/python\\n" exactly."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with unittest.mock.patch.object(rv, "ensure_cadre_dirs", return_value=Path("/fake/cadre")):
            with unittest.mock.patch.object(rv, "write_config"):
                with unittest.mock.patch.object(rv, "seed_starter_fleets") as fleet_mock:
                    with unittest.mock.patch.object(rv, "seed_personas") as persona_mock:
                        with contextlib.redirect_stdout(stdout_buf):
                            with contextlib.redirect_stderr(stderr_buf):
                                code = rv.main(["--venv-python", "/some/python"])

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue(), "/some/python\n",
                         "stdout must be EXACTLY the path + newline (nothing else)")
        # Guard both seeding call sites.
        repo_root = Path(rv.__file__).resolve().parents[1]
        fleet_mock.assert_called_once_with(repo_root, Path("/fake/cadre"))
        persona_mock.assert_called_once_with(repo_root, Path("/fake/cadre"))

    def test_diagnostics_go_to_stderr_not_stdout(self):
        """Scaffold confirmations appear on stderr, not stdout."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with unittest.mock.patch.object(rv, "ensure_cadre_dirs", return_value=Path("/fake/cadre")):
            with unittest.mock.patch.object(rv, "write_config"):
                with unittest.mock.patch.object(rv, "seed_starter_fleets") as fleet_mock:
                    with unittest.mock.patch.object(rv, "seed_personas") as persona_mock:
                        with contextlib.redirect_stdout(stdout_buf):
                            with contextlib.redirect_stderr(stderr_buf):
                                rv.main(["--venv-python", "/some/python"])

        # stdout has only the path; stderr has the diagnostic messages
        self.assertEqual(stdout_buf.getvalue().strip(), "/some/python")
        # Diagnostics (scaffold/config lines) go to stderr
        self.assertIn("cadre", stderr_buf.getvalue())
        repo_root = Path(rv.__file__).resolve().parents[1]
        fleet_mock.assert_called_once_with(repo_root, Path("/fake/cadre"))
        persona_mock.assert_called_once_with(repo_root, Path("/fake/cadre"))


# ---------------------------------------------------------------------------
# New tests: seed_starter_fleets
# ---------------------------------------------------------------------------


class TestSeedStarterFleets(unittest.TestCase):
    """seed_starter_fleets seeds fleets idempotently, owner-only, without stdout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cadre_home = Path(self.tmp) / "cadre"
        self.cadre_home.mkdir(mode=0o700)
        # Real repo root: scripts/resolve_venv.py → parents[1] is the repo root
        self.repo_root = Path(rv.__file__).resolve().parents[1]

    def test_clean_dir_seeds_both_fleets(self):
        """All starter fleets are seeded with .example stripped; palette is NOT seeded.

        Named "both" historically (two starters shipped first); the allowlist
        now carries seven (research-swarm, code-review, doc-review, review-scoring,
        research-brief, debate, critique-revise).
        Each new starter fleet must be added here.
        """
        rv.seed_starter_fleets(self.repo_root, self.cadre_home)
        self.assertTrue((self.cadre_home / "fleets" / "research-swarm.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "code-review.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "doc-review.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "review-scoring.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "research-brief.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "debate.yaml").exists())
        self.assertTrue((self.cadre_home / "fleets" / "critique-revise.yaml").exists())

    def test_non_utf8_source_warned_and_skipped_no_raise(self):
        """A non-UTF-8 source fleet is warned-and-skipped, never raised (never-raises contract)."""
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo)
        (repo / "fleets").mkdir()
        (repo / "fleets" / "research-swarm.example.yaml").write_bytes(b"\xff\xfe not utf-8 \x80")
        # Must not raise even though the source is undecodable.
        rv.seed_starter_fleets(repo, self.cadre_home)
        # The undecodable source was skipped — no destination written.
        self.assertFalse((self.cadre_home / "fleets" / "research-swarm.yaml").exists())

    def test_symlink_at_destination_not_followed(self):
        """A symlink planted at the destination is not followed/overwritten (O_EXCL/O_NOFOLLOW)."""
        fleets_dir = self.cadre_home / "fleets"
        fleets_dir.mkdir(mode=0o700, exist_ok=True)
        sentinel = self.cadre_home / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET")
        (fleets_dir / "research-swarm.yaml").symlink_to(sentinel)
        rv.seed_starter_fleets(self.repo_root, self.cadre_home)  # must not raise
        # The symlink target is untouched — seeding refused to follow the link and write through.
        self.assertEqual(sentinel.read_text(), "OPERATOR SECRET")

    def test_palette_not_seeded(self):
        """palette.yaml and palette.example.yaml must NOT be seeded."""
        rv.seed_starter_fleets(self.repo_root, self.cadre_home)
        self.assertFalse((self.cadre_home / "fleets" / "palette.yaml").exists())
        self.assertFalse((self.cadre_home / "fleets" / "palette.example.yaml").exists())

    def test_idempotent_preserves_operator_edits(self):
        """A pre-existing fleet file is NOT overwritten (operator edits preserved)."""
        fleets_dir = self.cadre_home / "fleets"
        fleets_dir.mkdir(mode=0o700, exist_ok=True)
        sentinel_path = fleets_dir / "research-swarm.yaml"
        sentinel_path.write_text("OPERATOR EDIT", encoding="utf-8")

        rv.seed_starter_fleets(self.repo_root, self.cadre_home)

        self.assertEqual(sentinel_path.read_text(encoding="utf-8"), "OPERATOR EDIT")

    def test_stdout_silence(self):
        """seed_starter_fleets writes NOTHING to stdout."""
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            rv.seed_starter_fleets(self.repo_root, self.cadre_home)
        self.assertEqual(stdout_buf.getvalue(), "")

    def test_seeded_file_permissions_0o600(self):
        """Freshly seeded fleet files are owner-only (0o600)."""
        rv.seed_starter_fleets(self.repo_root, self.cadre_home)
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
            rv.seed_starter_fleets(self.repo_root, bad_home)
        self.assertEqual(stdout_buf.getvalue(), "")


class TestSeedPersonas(unittest.TestCase):
    """seed_personas seeds persona files idempotently, owner-only, without stdout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cadre_home = Path(self.tmp) / "cadre"
        self.cadre_home.mkdir(mode=0o700)
        # Real repo root: scripts/resolve_venv.py → parents[1] is the repo root
        self.repo_root = Path(rv.__file__).resolve().parents[1]

    def test_clean_dir_seeds_all_five_personas(self):
        """All five persona files are seeded with names unchanged (no .example strip)."""
        rv.seed_personas(self.repo_root, self.cadre_home)
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
        rv.seed_personas(self.repo_root, self.cadre_home)
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

        rv.seed_personas(self.repo_root, self.cadre_home)

        self.assertEqual(sentinel_path.read_text(encoding="utf-8"), "OPERATOR EDIT")

    def test_symlink_at_destination_not_followed(self):
        """A symlink planted at the destination is not followed/overwritten (O_EXCL/O_NOFOLLOW)."""
        personas_dir = self.cadre_home / "personas"
        personas_dir.mkdir(mode=0o700, exist_ok=True)
        sentinel = self.cadre_home / "sentinel.txt"
        sentinel.write_text("OPERATOR SECRET")
        (personas_dir / "coherence-reviewer.md").symlink_to(sentinel)
        rv.seed_personas(self.repo_root, self.cadre_home)  # must not raise
        # The symlink target is untouched — seeding refused to follow the link and write through.
        self.assertEqual(sentinel.read_text(), "OPERATOR SECRET")

    def test_non_utf8_source_warned_and_skipped_no_raise(self):
        """A non-UTF-8 source persona is warned-and-skipped, never raised (never-raises contract)."""
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(repo))
        (repo / "personas").mkdir()
        (repo / "personas" / "coherence-reviewer.md").write_bytes(b"\xff\xfe not utf-8 \x80")
        # Must not raise even though the source is undecodable.
        rv.seed_personas(repo, self.cadre_home)
        # The undecodable source was skipped — no destination written.
        self.assertFalse((self.cadre_home / "personas" / "coherence-reviewer.md").exists())

    def test_stdout_silence(self):
        """seed_personas writes NOTHING to stdout."""
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            rv.seed_personas(self.repo_root, self.cadre_home)
        self.assertEqual(stdout_buf.getvalue(), "")

    def test_never_raises_on_bad_cadre_home(self):
        """seed_personas does NOT raise when cadre_home is unwritable/nonexistent."""
        bad_home = Path("/nonexistent/unwritable/xyz")
        stdout_buf = io.StringIO()
        # Must not raise; must not write to stdout
        with contextlib.redirect_stdout(stdout_buf):
            rv.seed_personas(self.repo_root, bad_home)
        self.assertEqual(stdout_buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
