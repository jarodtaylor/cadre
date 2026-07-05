"""Tests for scripts/resolve_venv.py — the minimal Hermes-venv-Python resolver.

All tests use tempfile paths. The real ~/.cadre is NEVER touched (this script
no longer scaffolds/seeds/writes config at all — see tests/test_provision.py
and tests/test_cli.py::TestSetupCommand* for that, now cadre.provision /
`cadre setup`, U4).

Loads the module via importlib (scripts/ is not a Python package),
mirroring the by-path import pattern in test_cli.py:_load_skill_module.
"""

import contextlib
import importlib.util
import io
import shutil
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


# ---------------------------------------------------------------------------
# main() stdout/stderr contract (install.sh PYBIN capture)
# ---------------------------------------------------------------------------


class TestMainStdoutContract(unittest.TestCase):
    """main() prints ONLY the resolved python path to stdout; nothing else.

    This locks the `PYBIN="$(python3 scripts/resolve_venv.py "$@")"` contract in
    install.sh: command substitution captures stdout, so any stray text there
    would corrupt PYBIN. Since U4, main() ONLY resolves + prints — scaffolding,
    seeding, and config-writing moved to `cadre setup` (cadre.provision), so
    there is nothing left to mock here.
    """

    def test_explicit_venv_python_stdout_is_only_path(self):
        """main(["--venv-python", "/some/python"]) stdout == "/some/python\\n" exactly."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with contextlib.redirect_stdout(stdout_buf):
            with contextlib.redirect_stderr(stderr_buf):
                code = rv.main(["--venv-python", "/some/python"])

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout_buf.getvalue(),
            "/some/python\n",
            "stdout must be EXACTLY the path + newline (nothing else)",
        )
        self.assertEqual(stderr_buf.getvalue(), "", "no diagnostics expected on the success path")

    def test_error_path_writes_to_stderr_not_stdout(self):
        """A resolution failure prints the error to stderr; stdout stays empty; exit 1.

        Mocks resolve_venv itself (rather than mutating the real os.environ / probe
        paths) so this stays independent of whatever CADRE_HERMES_PYTHON happens to
        be set to in the ambient test environment.
        """
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with unittest.mock.patch.object(
            rv, "resolve_venv", side_effect=RuntimeError("no python found anywhere")
        ):
            with contextlib.redirect_stdout(stdout_buf):
                with contextlib.redirect_stderr(stderr_buf):
                    code = rv.main([])

        self.assertEqual(code, 1)
        self.assertEqual(stdout_buf.getvalue(), "", "stdout must stay empty on failure")
        self.assertIn("no python found anywhere", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
