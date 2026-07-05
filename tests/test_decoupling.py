"""tests/test_decoupling.py — the KTD7 subprocess de-coupling + wheel-seed proof
(U7 of docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md).

Deliberately NOT a unit test (KTD7). Importing `cadre` in-process, in the SAME
interpreter that is running `unittest discover -s tests` from the repo root,
resolves `sys.path` the way R9 deliberately allows for dev (cwd on sys.path,
no install needed) — that is exactly the resolution this file must NOT lean
on, because it would mask the one bug class this file exists to catch: a
wheel that omits part of `cadre/data/**` (KTD9's risk), or a shipped skill
runner that still reaches for the repo instead of the installed package
(R15). Proving R14 needs a REAL subprocess: a wheel built from this checkout,
installed into a throwaway venv, exercised from a neutral cwd outside the
repo so no cwd-relative resolution can reach the source tree.

setUpClass builds that wheel and venv once for the whole class — slower under
a cold pip cache (fetching the hatchling build backend and pyyaml from PyPI),
fast when cached (a few hundred ms, measured locally). If any step can't
complete — no network, pip/venv unavailable, a packaging error — the WHOLE
CLASS self-skips LOUDLY via unittest.SkipTest, with the actual subprocess
output folded into the reason, so a CI misconfiguration shows up as a visible
skip, never a silent pass. Past setUpClass, the test methods' assertions do
NOT weaken for this: if the wheel is missing package data, or the shipped
run.py's imports don't resolve, the test FAILS — that is this file's job.

Cleanup uses addClassCleanup, not tearDownClass: unittest does not run
tearDownClass when setUpClass raises (verified empirically — SkipTest
included), so a skip raised partway through setUpClass would otherwise leak
the venv/tempdirs already created. Registering each resource's cleanup right
after creating it guarantees cleanup on every path: full success, a skip at
any step, or an unexpected error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cadre import provision

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Generous but bounded: a cold pip cache fetching the hatchling build backend
# and pyyaml from PyPI is the slow path this guards; a genuine HANG (not mere
# slowness) is what must not wedge the suite. All comfortably inside the CI
# job's 15-minute timeout (.github/workflows/test.yml).
_WHEEL_BUILD_TIMEOUT_S = 180.0
_VENV_CREATE_TIMEOUT_S = 60.0
_WHEEL_INSTALL_TIMEOUT_S = 180.0
_SETUP_TIMEOUT_S = 60.0
_SUBPROCESS_TIMEOUT_S = 30.0

# A focus-only starter fleet (no persona: references) — cadre.personas.resolve
# does zero pool-directory I/O for a focus-only spec, so `cadre validate`
# succeeds regardless of whether a persona pool exists at the neutral cwd.
_VALIDATE_FLEET = "research-swarm.example.yaml"


def _venv_bin(venv_dir: Path, name: str) -> str:
    """Path to an executable inside a venv, cross-platform."""
    if os.name == "nt":
        return str(venv_dir / "Scripts" / f"{name}.exe")
    return str(venv_dir / "bin" / name)


class TestDecoupling(unittest.TestCase):
    """KTD7: `import cadre`, the `cadre` console command, the shipped skill
    run.py, and `cadre setup` all work off a REAL pip-installed wheel with the
    repo absent from every subprocess's view (a neutral cwd for each call)."""

    @classmethod
    def setUpClass(cls):
        workdir = Path(tempfile.mkdtemp(prefix="cadre-decoupling-"))
        cls.addClassCleanup(shutil.rmtree, str(workdir), ignore_errors=True)

        wheel_dir = workdir / "wheel"
        wheel_dir.mkdir()

        # Build the wheel from THIS checkout. --no-deps: we only need the cadre
        # wheel itself, not wheels for its dependencies too; pip still fetches
        # the hatchling build backend (declared in [build-system] requires) on
        # its own via PEP 517 build isolation, so no separate `build` package
        # install is needed here.
        try:
            build = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(wheel_dir)],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=_WHEEL_BUILD_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise unittest.SkipTest(
                f"pip wheel timed out after {_WHEEL_BUILD_TIMEOUT_S}s building the cadre "
                f"wheel -- no network to fetch the hatchling build backend? ({exc})"
            ) from None
        except OSError as exc:
            raise unittest.SkipTest(f"could not invoke `pip wheel` at all: {exc}") from None

        if build.returncode != 0:
            raise unittest.SkipTest(
                "`pip wheel . --no-deps` failed building the cadre wheel (no network to "
                f"fetch the hatchling build backend, or a packaging error) -- exit "
                f"{build.returncode}:\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
            )

        wheels = sorted(wheel_dir.glob("cadre-*.whl"))
        if not wheels:
            raise unittest.SkipTest(
                f"`pip wheel` exited 0 but produced no cadre-*.whl in {wheel_dir} -- "
                f"stdout:\n{build.stdout}"
            )
        wheel_path = wheels[0]

        # A throwaway venv, then install the built wheel into it.
        venv_dir = workdir / "venv"
        try:
            venv_create = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=_VENV_CREATE_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise unittest.SkipTest(f"could not create a throwaway venv: {exc}") from None
        if venv_create.returncode != 0:
            raise unittest.SkipTest(
                f"venv creation failed -- exit {venv_create.returncode}:\n"
                f"--- stdout ---\n{venv_create.stdout}\n--- stderr ---\n{venv_create.stderr}"
            )

        venv_python = _venv_bin(venv_dir, "python")

        # Deliberately NOT --no-deps here (unlike the wheel build above): pyyaml
        # is a real runtime dependency (R4) -- cadre.config imports it at module
        # level -- so `cadre validate` below needs it actually installed.
        try:
            install = subprocess.run(
                [venv_python, "-m", "pip", "install", str(wheel_path)],
                capture_output=True,
                text=True,
                timeout=_WHEEL_INSTALL_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise unittest.SkipTest(
                f"pip install of the built wheel into the throwaway venv failed to run "
                f"(no network to fetch pyyaml?): {exc}"
            ) from None
        if install.returncode != 0:
            raise unittest.SkipTest(
                f"pip install of the built cadre wheel failed -- exit {install.returncode}:\n"
                f"--- stdout ---\n{install.stdout}\n--- stderr ---\n{install.stderr}"
            )

        cls.venv_python = venv_python
        cls.venv_cadre = _venv_bin(venv_dir, "cadre")

        # A neutral cwd OUTSIDE the repo for every subprocess call in the test
        # methods below -- the KTD7 trap: Python inserts '' (cwd) at sys.path[0]
        # for `-c` invocations, so running any of these checks from inside the
        # repo would silently resolve `cadre` from the SOURCE TREE even with the
        # wheel-installed venv's python -- masking exactly the bug (an omitted
        # cadre/data/** glob, or a run.py that still reaches for the repo) this
        # file exists to catch. (Confirmed by hand while building this test: an
        # early draft that resolved skill_dir() with the ambient repo-root cwd
        # silently returned the repo's cadre/data/skill, not the venv's.)
        neutral_cwd = Path(tempfile.mkdtemp(prefix="cadre-decoupling-cwd-"))
        cls.addClassCleanup(shutil.rmtree, str(neutral_cwd), ignore_errors=True)
        cls.neutral_cwd = str(neutral_cwd)

        # A focus-only starter fleet, copied out of the repo into the neutral
        # cwd, so `cadre validate` reads a file that happens to sit next to it
        # -- not one reached via a repo-relative path.
        fleet_src = _REPO_ROOT / "cadre" / "data" / "fleets" / _VALIDATE_FLEET
        cls.fleet_copy_name = "research-swarm.yaml"
        (neutral_cwd / cls.fleet_copy_name).write_text(
            fleet_src.read_text(encoding="utf-8"), encoding="utf-8"
        )

    def test_import_cadre_resolves_from_installed_wheel(self):
        """`import cadre` succeeds under the venv python, from a cwd outside the repo."""
        result = subprocess.run(
            [self.venv_python, "-c", "import cadre; print(cadre.__file__)"],
            cwd=self.neutral_cwd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Positive half of the KTD7 proof: resolves into the venv's
        # site-packages. Negative half: the repo checkout path never appears.
        self.assertIn("site-packages", result.stdout)
        self.assertNotIn(str(_REPO_ROOT), result.stdout)

    def test_cadre_validate_from_outside_repo(self):
        """`cadre validate` (the installed console command, R2) exits 0 from
        outside the repo -- reproduces validate with no clone present."""
        result = subprocess.run(
            [self.venv_cadre, "validate", self.fleet_copy_name],
            cwd=self.neutral_cwd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertIn("OK: research-swarm", result.stdout)

    def test_shipped_run_py_imports_resolve(self):
        """The shipped skill run.py's top-level imports resolve under the
        installed package (R15), located under the venv's site-packages (not
        the repo), with the repo absent from this process's view."""
        resolve = subprocess.run(
            [self.venv_python, "-c", "from cadre.resources import skill_dir; print(skill_dir())"],
            cwd=self.neutral_cwd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        self.assertEqual(resolve.returncode, 0, resolve.stderr)
        skill_dir = resolve.stdout.strip()
        self.assertIn("site-packages", skill_dir)
        self.assertNotIn(str(_REPO_ROOT), skill_dir)

        run_py = os.path.join(skill_dir, "run.py")
        result = subprocess.run(
            [self.venv_python, run_py, "--help"],
            cwd=self.neutral_cwd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        self.assertEqual(
            result.returncode,
            0,
            "shipped run.py --help failed -- an import likely didn't resolve:\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        self.assertIn("usage", result.stdout.lower())

    def test_setup_seeds_from_wheel_data_in_fresh_home(self):
        """The KTD7 seam: `cadre setup` against a fresh $HOME seeds the starter
        fleets, personas, and the palette-candidates file FROM THE WHEEL's
        package data. Fails if the wheel omitted any of cadre/data/** --
        _seed_files warns-and-skips per missing/unreadable source (never
        raises), so an omitted glob shows up here as a shorter file list, not
        a crash -- which is exactly what the assertEqual calls below catch.
        """
        expected_fleets = sorted(
            name.replace(".example.yaml", ".yaml") for name in provision._STARTER_FLEETS
        )
        expected_personas = sorted(provision._STARTER_PERSONAS)
        # Sanity-check the source of truth itself, independent of what actually
        # got seeded -- names the exact counts U7 was asked to prove present.
        self.assertEqual(len(expected_fleets), 7)
        self.assertEqual(len(expected_personas), 5)

        with tempfile.TemporaryDirectory(prefix="cadre-decoupling-home-") as fresh_home:
            env = os.environ.copy()
            env["HOME"] = fresh_home
            env["CADRE_HERMES_PYTHON"] = self.venv_python

            result = subprocess.run(
                [self.venv_cadre, "setup"],
                cwd=self.neutral_cwd,
                capture_output=True,
                text=True,
                timeout=_SETUP_TIMEOUT_S,
                env=env,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"cadre setup failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}",
            )

            cadre_home = Path(fresh_home) / ".cadre"

            actual_fleets = sorted(p.name for p in (cadre_home / "fleets").glob("*.yaml"))
            self.assertEqual(actual_fleets, expected_fleets)

            actual_personas = sorted(p.name for p in (cadre_home / "personas").glob("*.md"))
            self.assertEqual(actual_personas, expected_personas)

            palette_candidates = cadre_home / "palette-candidates.yaml"
            self.assertTrue(palette_candidates.is_file())
            self.assertGreater(palette_candidates.stat().st_size, 0)

            # Bonus: KTD11's config write also resolves end-to-end from the wheel.
            config_path = cadre_home / "config"
            self.assertTrue(config_path.is_file())
            self.assertIn(
                f"CADRE_HERMES_PYTHON={self.venv_python}",
                config_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
