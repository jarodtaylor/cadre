import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from cadre.approval import consume_approval, surface_digest, write_approval
from cadre.capture import _slugify, resolve_run_dir, resolved_hermes_home
from cadre.cli import main as cli_main, run_command, setup_command, validate_command
from cadre.config import FleetConfig
from cadre.file_input import compose
from cadre.model_client import AgentResult
from cadre.personas import default_pool_dir, resolve
from cadre.resources import fleets_dir, skill_dir

EXAMPLE = str(fleets_dir() / "research-swarm.example.yaml")

# A real run now streams [cadre] progress breadcrumbs to sys.stderr — the same
# stream unittest uses for its own dots/summary. No test in this module asserts on
# stderr, so silence it module-wide to keep the suite output clean. Tests that need
# to inspect progress pass an explicit progress_stream (cli) or redirect_stderr
# locally (skill). The runner captured the real stderr before setUpModule ran, so
# failure output is unaffected.
_REAL_STDERR = None


def setUpModule():
    global _REAL_STDERR
    _REAL_STDERR = sys.stderr
    sys.stderr = io.StringIO()


def tearDownModule():
    if _REAL_STDERR is not None:
        sys.stderr = _REAL_STDERR


def _tmp_yaml(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


class FakeClient:
    """Fake ModelClient that records calls and supports configurable behavior."""

    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.calls = []  # list of (role, provider, model) tuples for call-count assertions

    def run(self, *, role, provider, model, prompt, toolset=()):
        self.calls.append((role, provider, model))
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(role=role, provider=provider, model=model, ok=ok,
                           text=payload if ok else None, error=None if ok else payload)


# ---------------------------------------------------------------------------
# Existing validate tests (unchanged)
# ---------------------------------------------------------------------------


class TestValidate(unittest.TestCase):
    def test_description_shown_in_validate(self):
        path = _tmp_yaml(
            "name: d-fleet\n"
            "description: A catalog blurb for this fleet\n"
            "synthesis: {provider: openrouter, model: m}\n"
            "specialists:\n"
            "  - {role: r, provider: openrouter, model: m, toolset: [], focus: analysis}\n"
        )
        self.addCleanup(os.unlink, path)
        code, out = validate_command(path)
        self.assertEqual(code, 0)
        self.assertIn("A catalog blurb for this fleet", out)

    def test_validate_output_sanitizes_fleet_fields(self):
        """A terminal escape in a fleet field must not reach validate output (approval surface)."""
        path = _tmp_yaml(
            "name: d-fleet\n"
            'description: "blurb\\u001b[2Kevil"\n'
            "synthesis: {provider: openrouter, model: m}\n"
            "specialists:\n"
            "  - {role: r, provider: openrouter, model: m, toolset: [], focus: analysis}\n"
        )
        self.addCleanup(os.unlink, path)
        code, out = validate_command(path)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)

    def test_valid_example_passes(self):
        code, out = validate_command(EXAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("OK: research-swarm", out)
        self.assertIn("synthesis:", out)

    def test_invalid_spec_fails(self):
        path = _tmp_yaml("name: broken\nspecialists: []\n")  # missing synthesis, empty specialists
        self.addCleanup(os.unlink, path)
        code, out = validate_command(path)
        self.assertEqual(code, 1)
        self.assertIn("Invalid fleet config", out)
        self.assertIn("synthesis", out)

    def test_validate_syntactically_invalid_yaml(self):
        path = _tmp_yaml("name: [unterminated\n")
        self.addCleanup(os.unlink, path)
        code, out = validate_command(path)
        self.assertEqual(code, 1)
        self.assertIn("Invalid fleet config", out)


# ---------------------------------------------------------------------------
# Existing run tests — capture=False keeps them hermetic (no ~/.cadre writes)
# ---------------------------------------------------------------------------


class TestRun(unittest.TestCase):
    def test_run_renders_synthesis_and_provenance(self):
        client = FakeClient({"synthesizer": ("ok", "THE REPORT")})
        code, out = run_command(EXAMPLE, "what's new?", client=client, capture=False)
        self.assertEqual(code, 0)
        self.assertIn("THE REPORT", out)
        self.assertIn("--- provenance ---", out)
        for role in ("social", "web", "analysis"):
            self.assertIn(role, out)

    def test_run_renders_failures(self):
        client = FakeClient({"social": ("fail", "auth error"), "synthesizer": ("ok", "PARTIAL")})
        code, out = run_command(EXAMPLE, "task", client=client, capture=False)
        self.assertEqual(code, 0)
        self.assertIn("[FAIL] social", out)
        self.assertIn("auth error", out)
        self.assertIn("PARTIAL", out)

    def test_run_total_failure_exits_nonzero(self):
        client = FakeClient({r: ("fail", "down") for r in ("social", "web", "analysis")})
        code, out = run_command(EXAMPLE, "task", client=client, capture=False)
        self.assertEqual(code, 1)
        self.assertIn("partial result (no synthesis)", out)

    def test_run_synthesizer_failure_still_shows_specialist_text(self):
        # specialists succeed (default), synthesizer fails -> surviving lane output
        # must still reach the user, not just provenance rows.
        client = FakeClient({"synthesizer": ("fail", "rate limited")})
        code, out = run_command(EXAMPLE, "task", client=client, capture=False)
        self.assertEqual(code, 1)
        self.assertIn("social-output", out)
        self.assertIn("web-output", out)
        self.assertIn("synthesizer failed", out)


# ---------------------------------------------------------------------------
# U3 capture wiring tests — cli.py run_command
# ---------------------------------------------------------------------------


class TestRunCaptureDefault(unittest.TestCase):
    """Default run (capture=True) writes a folder and prints its path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_default_run_writes_folder_and_prints_path(self):
        run_dir = self.tmp / "myrun"
        client = FakeClient({"synthesizer": ("ok", "SYNTH")})
        code, out = run_command(EXAMPLE, "find best tools", client=client, run_dir=run_dir)
        self.assertEqual(code, 0)
        self.assertTrue(run_dir.exists(), "run_dir should be created")
        self.assertIn(str(run_dir), out, "output should include run folder path")

    def test_default_run_writes_manifest_json(self):
        run_dir = self.tmp / "myrun2"
        client = FakeClient({"synthesizer": ("ok", "SYNTH")})
        run_command(EXAMPLE, "find best tools", client=client, run_dir=run_dir)
        self.assertTrue((run_dir / "manifest.json").exists())

    def test_partial_failure_still_writes_folder(self):
        """A run where some specialists fail still captures (R13)."""
        run_dir = self.tmp / "partial"
        client = FakeClient({"social": ("fail", "auth error"), "synthesizer": ("ok", "PARTIAL")})
        code, out = run_command(EXAMPLE, "test task", client=client, run_dir=run_dir)
        self.assertEqual(code, 0)
        self.assertTrue(run_dir.exists())
        self.assertTrue((run_dir / "manifest.json").exists())


class TestRunCaptureDisabled(unittest.TestCase):
    """--no-capture / capture=False writes no folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_no_capture_writes_no_folder(self):
        run_dir = self.tmp / "should-not-exist"
        client = FakeClient({"synthesizer": ("ok", "SYNTH")})
        code, out = run_command(EXAMPLE, "task", client=client, run_dir=run_dir, capture=False)
        self.assertEqual(code, 0)
        self.assertFalse(run_dir.exists(), "run_dir should NOT be created when capture=False")


class TestRunCaptureBadDir(unittest.TestCase):
    """A bad or unwritable run-dir fails fast: no model calls, non-zero exit, clear message."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_unwritable_dir_fails_fast_before_model_calls(self):
        # Make a regular file where run_dir would need to be a directory.
        blocker = self.tmp / "blocker"
        blocker.write_text("I am a file, not a dir")
        bad_dir = blocker / "subdir"  # can't mkdir under a file

        client = FakeClient({"synthesizer": ("ok", "SYNTH")})
        code, out = run_command(EXAMPLE, "task", client=client, run_dir=bad_dir)

        self.assertEqual(code, 1, "should return non-zero on dir creation failure")
        self.assertIn("--no-capture", out, "error message should mention --no-capture")
        self.assertEqual(len(client.calls), 0, "no model calls should be made (fail-fast)")

    def test_unwritable_dir_message_mentions_run_directory(self):
        blocker = self.tmp / "blocker2"
        blocker.write_text("blocking file")
        bad_dir = blocker / "subdir"

        client = FakeClient()
        code, out = run_command(EXAMPLE, "task", client=client, run_dir=bad_dir)
        self.assertIn("Cannot create run directory", out)


class TestRunCaptureSaveRunFailure(unittest.TestCase):
    """A post-run save_run failure still returns synthesis and normal exit code (R16)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_save_run_failure_does_not_discard_synthesis(self):
        run_dir = self.tmp / "writerun"
        client = FakeClient({"synthesizer": ("ok", "MY SYNTHESIS")})

        with patch("cadre.cli.save_run", side_effect=OSError("disk full")):
            code, out = run_command(EXAMPLE, "task", client=client, run_dir=run_dir)

        # The run itself succeeded — exit code 0, synthesis visible
        self.assertEqual(code, 0)
        self.assertIn("MY SYNTHESIS", out)

    def test_save_run_failure_exit_code_matches_run_outcome(self):
        """Non-zero exit from a failed run is preserved even when save_run also fails."""
        run_dir = self.tmp / "writerun2"
        client = FakeClient({r: ("fail", "down") for r in ("social", "web", "analysis")})

        with patch("cadre.cli.save_run", side_effect=OSError("disk full")):
            code, out = run_command(EXAMPLE, "task", client=client, run_dir=run_dir)

        self.assertEqual(code, 1)  # run failed → non-zero even if save_run also failed

    def test_run_folder_line_uses_save_run_return(self):
        """Gate (#4): cli prints the path save_run RETURNS (the possibly-renamed
        dir), not the one it passed in. A caller that drops the return fails this."""
        run_dir = self.tmp / "passed-in"
        sentinel = Path("/cadre-sentinel/2026-06-29-150719-renamed-by-title")
        client = FakeClient({"synthesizer": ("ok", "S")})
        with patch("cadre.cli.save_run", return_value=sentinel):
            _, out = run_command(EXAMPLE, "task", client=client, run_dir=run_dir)
        self.assertIn(f"Run folder: {sentinel}", out)
        self.assertNotIn(f"Run folder: {run_dir}", out)


class TestRunCaptureRunDirPermissions(unittest.TestCase):
    """The run dir is created owner-only (0o700)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_run_dir_created_0o700(self):
        run_dir = self.tmp / "permrun"
        client = FakeClient({"synthesizer": ("ok", "SYNTH")})
        run_command(EXAMPLE, "find best tools", client=client, run_dir=run_dir)
        mode = stat.S_IMODE(run_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)


class TestRunCaptureUmaskParentDirs(unittest.TestCase):
    """FIX 4: newly-created parent directories (default path) are 0o700, not 0o755."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_default_path_parent_dirs_are_0o700(self):
        """When CADRE_RUN_DIR is unset, the auto-created default root is 0o700."""
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch("cadre.capture._DEFAULT_RUNS_ROOT", str(self.tmp / "cadre" / "runs")):
            with patch.dict(os.environ, env_without, clear=True):
                client = FakeClient({"synthesizer": ("ok", "SYNTH")})
                run_command(EXAMPLE, "find tools", client=client)
                # Check that the newly created cadre/runs parent dir is 0o700
                cadre_dir = self.tmp / "cadre"
                self.assertTrue(cadre_dir.exists(), "cadre parent dir should have been created")
                mode = stat.S_IMODE(cadre_dir.stat().st_mode)
                self.assertEqual(mode, 0o700, f"cadre parent dir should be 0o700, got 0o{mode:03o}")


# ---------------------------------------------------------------------------
# resolve_run_dir and _slugify tests
# ---------------------------------------------------------------------------


class TestResolveRunDir(unittest.TestCase):
    """resolve_run_dir uses CADRE_RUN_DIR when set, else builds a stamped default."""

    def test_cadre_run_dir_env_used_verbatim(self):
        with patch.dict(os.environ, {"CADRE_RUN_DIR": "/tmp/my-cadre-runs"}):
            result = resolve_run_dir("some task")
        self.assertEqual(result, Path("/tmp/my-cadre-runs"))

    def test_cadre_run_dir_expanduser(self):
        with patch.dict(os.environ, {"CADRE_RUN_DIR": "~/my-cadre-runs"}):
            result = resolve_run_dir("some task")
        self.assertEqual(result, Path("~/my-cadre-runs").expanduser())

    def test_default_resolves_under_cadre_runs(self):
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch.dict(os.environ, env_without, clear=True):
            result = resolve_run_dir("find best AI tools")
        cadre_runs = Path("~/.cadre/runs").expanduser()
        self.assertTrue(
            str(result).startswith(str(cadre_runs)),
            f"Expected path under {cadre_runs}, got {result}",
        )

    def test_default_leaf_contains_slug(self):
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch.dict(os.environ, env_without, clear=True):
            result = resolve_run_dir("find best AI tools")
        # The leaf should include the slugified form of the task
        self.assertIn("find-best-ai-tools", result.name)

    def test_no_cadre_run_dir_does_not_write_to_home(self):
        """resolve_run_dir returns a Path but does NOT create it — just computes it."""
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_RUN_DIR"}
        with patch.dict(os.environ, env_without, clear=True):
            result = resolve_run_dir("some task for path check")
        # The function must not create the directory
        self.assertFalse(result.exists(), "resolve_run_dir must not create the directory")


class TestSlugify(unittest.TestCase):
    """_slugify produces safe, bounded slugs (R18)."""

    def test_simple_task(self):
        self.assertEqual(_slugify("find best AI tools"), "find-best-ai-tools")

    def test_hostile_path_traversal(self):
        slug = _slugify("../../etc/passwd")
        self.assertNotIn("/", slug)
        self.assertNotIn("..", slug)
        # The resolved path with this slug cannot escape the runs dir
        run_dir = Path("/tmp/runs") / slug
        self.assertTrue(str(run_dir).startswith("/tmp/runs/"))

    def test_only_special_chars_falls_back_to_run(self):
        self.assertEqual(_slugify("!!!"), "run")

    def test_empty_string_falls_back_to_run(self):
        self.assertEqual(_slugify(""), "run")

    def test_max_length_40(self):
        long_task = "a" * 100
        slug = _slugify(long_task)
        self.assertLessEqual(len(slug), 40)

    def test_long_task_cuts_on_word_boundary(self):
        # A long multi-word task truncates at a word boundary, never mid-word
        # (regression: a hard [:40] cut sliced "inspired" -> "inspi").
        task = "research the advisor agent pattern inspired by claude code steps"
        slug = _slugify(task)
        self.assertLessEqual(len(slug), 40)
        words = set(task.split())
        for token in slug.split("-"):
            self.assertIn(token, words, f"{token!r} is a sliced-off partial word")

    def test_no_leading_trailing_dashes(self):
        slug = _slugify("  --hello world--  ")
        self.assertFalse(slug.startswith("-"))
        self.assertFalse(slug.endswith("-"))

    def test_uppercase_lowercased(self):
        self.assertEqual(_slugify("UPPER CASE"), "upper-case")

    def test_no_path_separators(self):
        for hostile in ("a/b", "a\\b", "../b", "a\x00b"):
            slug = _slugify(hostile)
            self.assertNotIn("/", slug, f"slug from {hostile!r} contains /")
            self.assertNotIn("..", slug, f"slug from {hostile!r} contains ..")


# ---------------------------------------------------------------------------
# Skill entry (cadre/data/skill/run.py) capture tests
# ---------------------------------------------------------------------------

def _load_skill_module():
    """Import cadre/data/skill/run.py as a module without running it."""
    skill_path = skill_dir() / "run.py"
    spec = importlib.util.spec_from_file_location("cadre_fleet_run", skill_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cadre_fleet_run"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSkillEntryCapture(unittest.TestCase):
    """cadre/data/skill/run.py captures identically to run_command."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_mod = _load_skill_module()

    def _run_skill(self, behavior=None, extra_argv=None):
        """Run the skill's main() with a fake client, redirected to self.tmp."""
        fake = FakeClient(behavior or {"synthesizer": ("ok", "SKILL SYNTH")})
        fleet_path = str(
            fleets_dir() / "research-swarm.example.yaml"
        )
        run_argv = ["--task", "what is best?", "--fleet", fleet_path] + (extra_argv or [])
        env = {"CADRE_RUN_DIR": str(self.tmp), "CADRE_APPROVAL_PATH": str(self.tmp / "approval")}

        with patch.dict(os.environ, env):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                # Mint the preview-bound approval for this exact surface first.
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                return self.run_mod.main(run_argv), fake

    def test_skill_default_writes_folder(self):
        code, fake = self._run_skill()
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "manifest.json").exists())

    def test_skill_no_capture_writes_no_folder(self):
        # Clear tmp and check nothing is written
        code, fake = self._run_skill(extra_argv=["--no-capture"])
        self.assertEqual(code, 0)
        # When --no-capture is set, manifest.json should not be written
        self.assertFalse((self.tmp / "manifest.json").exists())

    def test_skill_bad_dir_fails_fast_no_model_calls(self):
        """Skill: unwritable dir → exit 1, no model calls."""
        blocker = self.tmp / "blocker"
        blocker.write_text("I am a file")
        bad_dir = str(blocker / "subdir")
        fleet_path = str(
            fleets_dir() / "research-swarm.example.yaml"
        )
        run_argv = ["--task", "task", "--fleet", fleet_path]

        fake = FakeClient({"synthesizer": ("ok", "SYNTH")})
        env = {"CADRE_RUN_DIR": bad_dir, "CADRE_APPROVAL_PATH": str(self.tmp / "approval")}
        with patch.dict(os.environ, env):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                # Mint the preview-bound approval first, so the run below fails on
                # the BAD-DIR path (its intended target), not the approval gate.
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                code = self.run_mod.main(run_argv)

        self.assertEqual(code, 1)
        self.assertEqual(len(fake.calls), 0, "no model calls should be made on dir failure")

    def test_skill_save_run_failure_returns_synthesis(self):
        """Skill: save_run failure still returns exit 0 (run succeeded)."""
        with patch.object(self.run_mod, "save_run", side_effect=OSError("disk full")):
            code, fake = self._run_skill()
        self.assertEqual(code, 0)

    def test_run_folder_line_uses_save_run_return(self):
        """Gate (#4): the skill prints the path save_run RETURNS (the possibly-renamed
        dir), not the one it passed in. A caller that drops the return fails this."""
        sentinel = Path("/cadre-sentinel/2026-06-29-150719-renamed-by-title")
        fake = FakeClient({"synthesizer": ("ok", "S")})
        stdout_buf = io.StringIO()
        fleet_path = str(
            fleets_dir() / "research-swarm.example.yaml"
        )
        run_argv = ["--task", "t", "--fleet", fleet_path]
        env = {"CADRE_RUN_DIR": str(self.tmp), "CADRE_APPROVAL_PATH": str(self.tmp / "approval")}
        with patch.dict(os.environ, env):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                with patch.object(self.run_mod, "save_run", return_value=sentinel):
                    # Mint the preview-bound approval for this exact surface first.
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.run_mod.main(run_argv + ["--preview"])
                    with contextlib.redirect_stdout(stdout_buf):
                        self.run_mod.main(run_argv)
        out = stdout_buf.getvalue()
        self.assertIn(f"Run folder: {sentinel}", out)
        self.assertNotIn(f"Run folder: {self.tmp}", out)


# ---------------------------------------------------------------------------
# New cadre-fleet skill tests: preview, --fleet required, --task required
# ---------------------------------------------------------------------------

_EXAMPLE_FLEET = str(
    fleets_dir() / "research-swarm.example.yaml"
)


class TestSkillPreviewMakesNoModelCalls(unittest.TestCase):
    """--preview exits 0, makes ZERO model calls and ZERO capture side-effects.

    Covers R10: the preview short-circuit is BEFORE ModelClient and
    prepare_run_dir — proven by asserting both mocks are never called.
    """

    def setUp(self):
        self.run_mod = _load_skill_module()

    def test_preview_exits_zero(self):
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(stdout_buf):
                    code = self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview"])
        self.assertEqual(code, 0)

    def test_preview_makes_zero_model_calls(self):
        """ModelClient must never be instantiated in preview mode."""
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview"])
        fake_client_cls.assert_not_called()

    def test_preview_makes_zero_capture_calls(self):
        """prepare_run_dir must never be called in preview mode."""
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview"])
        mock_prepare.assert_not_called()

    def test_preview_output_contains_synthesizer_model(self):
        """Preview output must surface the synthesizer provider/model string."""
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", MagicMock()):
            with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview"])
        output = stdout_buf.getvalue()
        # The example fleet uses openrouter/anthropic/claude-opus-4.8 as synthesizer
        self.assertIn("openrouter", output)
        self.assertIn("claude-opus-4.8", output)

    def test_preview_output_contains_each_specialist_role(self):
        """Preview must list every specialist role."""
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", MagicMock()):
            with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview"])
        output = stdout_buf.getvalue()
        for role in ("social", "web", "analysis"):
            self.assertIn(role, output)

    def test_preview_output_contains_allow_privileged_tools(self):
        """Preview must surface the allow_privileged_tools value."""
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", MagicMock()):
            with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview"])
        output = stdout_buf.getvalue()
        self.assertIn("allow_privileged_tools", output)


class TestSkillPreviewMintsApprovalToken(unittest.TestCase):
    """--preview WITH --task mints a preview-bound approval token (U3 — the mint
    half; RUN-side enforcement is U4).

    Still zero model calls / zero capture (the existing invariant must not
    weaken); the minted token's digest matches an independently-computed
    ``surface_digest`` over the same (config, composed_task, profile); the
    composed task itself renders on the approval surface; different --doc
    content binds to a different digest; a task-less preview mints nothing
    (matches the run path's own None refusal); and a privileged fleet's PLAIN
    preview mints nothing (U5 adds the deliberate --approve-privileged mint).

    Every test isolates the token file via CADRE_APPROVAL_PATH so none of
    them ever touch the real ~/.cadre/approval.
    """

    def setUp(self):
        self.run_mod = _load_skill_module()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _token_path(self, name="approval"):
        return str(self.tmp / name)

    def test_preview_with_task_still_zero_model_calls(self):
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        stdout_buf = io.StringIO()
        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": self._token_path()}):
            with patch.object(self.run_mod, "ModelClient", fake_client_cls):
                with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                    with contextlib.redirect_stdout(stdout_buf):
                        code = self.run_mod.main(
                            ["--fleet", _EXAMPLE_FLEET, "--preview", "--task", "hello"]
                        )
        self.assertEqual(code, 0)
        fake_client_cls.assert_not_called()
        mock_prepare.assert_not_called()

    def test_preview_with_task_mints_bound_token(self):
        token_path = self._token_path()
        stdout_buf = io.StringIO()
        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": token_path}):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                    with contextlib.redirect_stdout(stdout_buf):
                        self.run_mod.main(
                            ["--fleet", _EXAMPLE_FLEET, "--preview", "--task", "hello"]
                        )
        token = consume_approval(path=token_path)
        self.assertIsNotNone(token)
        self.assertFalse(token.privileged)

        cfg = FleetConfig.load(_EXAMPLE_FLEET)
        resolve(cfg, default_pool_dir())
        composed_task, _doc_paths, _truncated = compose("hello", [])
        expected_digest = surface_digest(cfg, composed_task, resolved_hermes_home())
        self.assertEqual(token.digest, expected_digest)

    def test_preview_renders_composed_task(self):
        stdout_buf = io.StringIO()
        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": self._token_path()}):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                    with contextlib.redirect_stdout(stdout_buf):
                        self.run_mod.main(
                            [
                                "--fleet", _EXAMPLE_FLEET,
                                "--preview",
                                "--task", "a distinctive composed-task marker string",
                            ]
                        )
        output = stdout_buf.getvalue()
        self.assertIn("a distinctive composed-task marker string", output)

    def test_preview_with_doc_binds_doc_content(self):
        """Same --task, DIFFERENT --doc content -> different minted digest."""
        doc_a = self.tmp / "a.md"
        doc_a.write_text("content A")
        doc_b = self.tmp / "b.md"
        doc_b.write_text("content B")
        token_path_1 = self._token_path("approval1")
        token_path_2 = self._token_path("approval2")

        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": token_path_1}):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.run_mod.main(
                            ["--fleet", _EXAMPLE_FLEET, "--preview", "--task", "t", "--doc", str(doc_a)]
                        )
        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": token_path_2}):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.run_mod.main(
                            ["--fleet", _EXAMPLE_FLEET, "--preview", "--task", "t", "--doc", str(doc_b)]
                        )

        token_1 = consume_approval(path=token_path_1)
        token_2 = consume_approval(path=token_path_2)
        self.assertIsNotNone(token_1)
        self.assertIsNotNone(token_2)
        self.assertNotEqual(token_1.digest, token_2.digest)

    def test_taskless_preview_mints_no_token(self):
        """No --task and no --doc: composed_task is None, so preview renders the
        fleet shape only and mints nothing — the token file never gets created."""
        token_path = self._token_path()
        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": token_path}):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview"])
        self.assertFalse(os.path.exists(token_path))

    def test_privileged_fleet_plain_preview_mints_no_token(self):
        """A privileged fleet's plain --preview mints nothing — approving it
        requires the deliberate --approve-privileged act (U5, tested fully in
        TestSkillPrivilegedApproval; this test covers only the mint-nothing half)."""
        priv_fleet = self.tmp / "priv.yaml"
        priv_fleet.write_text(json.dumps({
            "name": "priv",
            "convergence": "collect",
            "specialists": [{"role": "a", "provider": "p", "model": "m", "focus": "f"}],
            "allow_privileged_tools": True,
        }))
        token_path = self._token_path()
        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": token_path}):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = self.run_mod.main(
                            ["--fleet", str(priv_fleet), "--preview", "--task", "hello"]
                        )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(token_path))


class TestSkillApprovalGate(unittest.TestCase):
    """U4 — the load-bearing security gate: a REAL run refuses to execute unless it
    presents a valid, unconsumed approval token bound to THIS exact surface (the
    config, the composed task, and the resolved profile). Mirrors the mint side
    tested in TestSkillPreviewMintsApprovalToken (U3).

    Every test isolates the token file via CADRE_APPROVAL_PATH so none of them
    ever touch the real ~/.cadre/approval.
    """

    def setUp(self):
        self.run_mod = _load_skill_module()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.token_path = str(self.tmp / "approval")

    def _env(self):
        return {"CADRE_APPROVAL_PATH": self.token_path}

    def test_no_approval_refused_zero_model_calls(self):
        """AE2: a run with NO prior --preview is refused; the message names
        --preview; ModelClient is never constructed (zero model calls)."""
        fake_client_cls = MagicMock()
        buf = io.StringIO()
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", fake_client_cls):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(
                        ["--fleet", _EXAMPLE_FLEET, "--task", "t", "--no-capture"]
                    )
        self.assertEqual(code, 1)
        self.assertIn("--preview", buf.getvalue())
        fake_client_cls.assert_not_called()

    def test_refused_run_makes_zero_capture(self):
        """The gate is fail-closed BEFORE capture: a run with no approval and
        capture ON (no --no-capture) never calls prepare_run_dir. Proves the gate
        sits ahead of every side effect, not just the model calls AE2 covers."""
        mock_prepare = MagicMock()
        buf = io.StringIO()
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                    with contextlib.redirect_stdout(buf):
                        code = self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--task", "t"])
        self.assertEqual(code, 1)
        mock_prepare.assert_not_called()

    def test_config_swap_refused(self):
        """AE1: --preview fleet A (mint), then run a DIFFERENT valid fleet B on the
        same approval path -> refused (digest mismatch), zero model calls."""
        fleet_b = str(
            fleets_dir() / "code-review.example.yaml"
        )
        fake_client_cls = MagicMock()
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", fake_client_cls):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--task", "t", "--preview"])
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(
                        ["--fleet", fleet_b, "--task", "t", "--no-capture"]
                    )
        self.assertEqual(code, 1)
        self.assertIn("--preview", buf.getvalue())
        fake_client_cls.assert_not_called()

    def test_task_swap_refused(self):
        """AE7: --preview with task T (mint), then run with a different task T' ->
        refused (digest mismatch), zero model calls."""
        fake_client_cls = MagicMock()
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", fake_client_cls):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(
                        ["--fleet", _EXAMPLE_FLEET, "--task", "original task", "--preview"]
                    )
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(
                        ["--fleet", _EXAMPLE_FLEET, "--task", "swapped task", "--no-capture"]
                    )
        self.assertEqual(code, 1)
        self.assertIn("--preview", buf.getvalue())
        fake_client_cls.assert_not_called()

    def test_profile_swap_refused(self):
        """The HERMES_HOME profile is a bound axis end-to-end: preview under one
        profile, run under another -> refused (digest mismatch), zero model calls.
        Fills the profile axis of the refusal matrix (config/task/one-shot are
        covered above); the pure-function digest test in test_approval only covers
        surface_digest, never run.py's HERMES_HOME env wiring. Absolute paths so the
        binding is cwd-independent."""
        fake_client_cls = MagicMock()
        # Two distinct absolute strings — never created/read, only fed to the digest as
        # the profile axis (kept off /tmp so the S108 hardcoded-temp lint doesn't flag
        # what is not actually a temp path — CodeRabbit).
        env_a = {**self._env(), "HERMES_HOME": "/nonexistent/cadre-profile-a"}
        env_b = {**self._env(), "HERMES_HOME": "/nonexistent/cadre-profile-b"}
        with patch.dict(os.environ, env_a):
            with patch.object(self.run_mod, "ModelClient", fake_client_cls):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--task", "t", "--preview"])
        buf = io.StringIO()
        with patch.dict(os.environ, env_b):
            with patch.object(self.run_mod, "ModelClient", fake_client_cls):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--task", "t", "--no-capture"])
        self.assertEqual(code, 1)
        self.assertIn("--preview", buf.getvalue())
        fake_client_cls.assert_not_called()

    def test_one_shot_reuse_second_run_refused(self):
        """AE3: --preview mints once; the first run consumes it and proceeds; a
        second run on the SAME approval path is refused (token already consumed)."""
        fake = FakeClient({"synthesizer": ("ok", "S")})
        run_argv = ["--fleet", _EXAMPLE_FLEET, "--task", "t", "--no-capture"]
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                with contextlib.redirect_stdout(io.StringIO()):
                    first_code = self.run_mod.main(run_argv)
                second_buf = io.StringIO()
                with contextlib.redirect_stdout(second_buf):
                    second_code = self.run_mod.main(run_argv)
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 1)
        self.assertIn("--preview", second_buf.getvalue())

    def test_match_proceeds_and_token_consumed(self):
        """A matching preview -> run proceeds (exit 0), and the token file no
        longer exists afterward — one-shot, even on the happy path."""
        fake = FakeClient({"synthesizer": ("ok", "S")})
        run_argv = ["--fleet", _EXAMPLE_FLEET, "--task", "t", "--no-capture"]
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                with contextlib.redirect_stdout(io.StringIO()):
                    code = self.run_mod.main(run_argv)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.token_path))

    def test_ae6_colluding_preview_then_run_proceeds(self):
        """AE6 — documented residual (Option-A scope; NOT a bug): --preview
        immediately followed by a run presenting the fresh matching token, with NO
        human step in between, PROCEEDS (exit 0). The binding proves the run
        matches what was previewed; it deliberately does NOT prove a human looked
        at the preview — that would be presence-detection, which is out of scope
        for this deployment (see SECURITY.md / the Part-2 plan's Option-A threat
        model: defend the honest-but-fallible agent + a tampered library; a fully
        colluding agent is a documented residual, not a bug). A future change
        must NOT "fix" this into presence-detection.
        """
        fake = FakeClient({"synthesizer": ("ok", "S")})
        run_argv = ["--fleet", _EXAMPLE_FLEET, "--task", "t", "--no-capture"]
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                with contextlib.redirect_stdout(io.StringIO()):
                    code = self.run_mod.main(run_argv)
        self.assertEqual(code, 0, "AE6: colluding preview-then-run proceeds by design")


class TestSkillPrivilegedApproval(unittest.TestCase):
    """U5 — a distinct --approve-privileged act. A fleet with
    allow_privileged_tools: true is NOT approved by a plain --preview; only the
    deliberate --approve-privileged act mints the privileged-flavored token the
    run gate requires for it. A non-privileged fleet accepts either flavor (no
    lock-out) — mirrors the mint/gate pair in TestSkillPreviewMintsApprovalToken
    (U3) / TestSkillApprovalGate (U4).

    Every test isolates the token file via CADRE_APPROVAL_PATH so none of them
    ever touch the real ~/.cadre/approval.
    """

    def setUp(self):
        self.run_mod = _load_skill_module()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.token_path = str(self.tmp / "approval")
        self.priv_fleet = self.tmp / "priv.yaml"
        with open(self.priv_fleet, "w") as f:
            yaml.safe_dump(
                {
                    "name": "priv",
                    "convergence": "collect",
                    "specialists": [
                        {"role": "a", "provider": "p", "model": "m", "focus": "f"}
                    ],
                    "allow_privileged_tools": True,
                },
                f,
            )

    def _env(self):
        return {"CADRE_APPROVAL_PATH": self.token_path}

    def test_plain_preview_of_privileged_fleet_mints_nothing_and_instructs(self):
        """A plain --preview of a privileged fleet mints no token and tells the
        operator how to approve it instead of silently doing nothing."""
        buf = io.StringIO()
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(
                        ["--fleet", str(self.priv_fleet), "--preview", "--task", "t"]
                    )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.token_path))
        self.assertIn("--approve-privileged", buf.getvalue())

    def test_approve_privileged_rerenders_privileged_preview(self):
        """--approve-privileged re-renders the SAME preview (the PRIVILEGED TOOLS
        ENABLED warning + the fleet's specialists), not just a silent mint — so a
        human approving a privileged fleet still sees the warning on THAT invocation.
        Guards against a refactor that short-circuits it to mint-only."""
        buf = io.StringIO()
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(
                        ["--fleet", str(self.priv_fleet), "--approve-privileged", "--task", "t"]
                    )
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("PRIVILEGED TOOLS ENABLED", out)
        self.assertIn("[a]", out)  # the fleet's specialist role, via render_fleet_preview

    def test_approve_privileged_mints_privileged_token(self):
        """--approve-privileged mints a token whose privileged flavor is True."""
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = self.run_mod.main(
                        ["--fleet", str(self.priv_fleet), "--approve-privileged", "--task", "t"]
                    )
        self.assertEqual(code, 0)
        token = consume_approval(path=self.token_path)
        self.assertIsNotNone(token)
        self.assertTrue(token.privileged)

    def test_privileged_fleet_run_with_normal_token_refused(self):
        """AE4: a NORMAL-flavored token bound to the exact privileged-fleet
        surface is still refused — the flavor check gates independently of the
        digest match. Zero model calls."""
        cfg = FleetConfig.load(str(self.priv_fleet))
        resolve(cfg, default_pool_dir())
        composed_task, _doc_paths, _truncated = compose("t", [])
        digest = surface_digest(cfg, composed_task, resolved_hermes_home())
        write_approval(digest, privileged=False, path=self.token_path)

        fake_client_cls = MagicMock()
        buf = io.StringIO()
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", fake_client_cls):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(
                        ["--fleet", str(self.priv_fleet), "--task", "t", "--no-capture"]
                    )
        self.assertEqual(code, 1)
        self.assertIn("--approve-privileged", buf.getvalue())
        fake_client_cls.assert_not_called()

    def test_privileged_fleet_run_with_privileged_token_proceeds(self):
        """--approve-privileged mint followed by a matching run proceeds."""
        fake = FakeClient({"a": ("ok", "A OUTPUT")})
        run_argv = ["--fleet", str(self.priv_fleet), "--task", "t", "--no-capture"]
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--approve-privileged"])
                with contextlib.redirect_stdout(io.StringIO()):
                    code = self.run_mod.main(run_argv)
        self.assertEqual(code, 0)

    def test_privileged_fleet_no_token_refusal_names_approve_privileged(self):
        """Copilot: a privileged fleet run with NO token hits the digest/absent
        refusal, whose message must point at --approve-privileged — a privileged
        fleet's plain --preview mints nothing, so telling the operator to --preview
        would loop them."""
        fake_client_cls = MagicMock()
        buf = io.StringIO()
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", fake_client_cls):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(
                        ["--fleet", str(self.priv_fleet), "--task", "t", "--no-capture"]
                    )
        self.assertEqual(code, 1)
        self.assertIn("--approve-privileged", buf.getvalue())
        fake_client_cls.assert_not_called()

    def test_nonprivileged_fleet_accepts_privileged_token_no_lockout(self):
        """A non-privileged fleet approved via --approve-privileged still runs —
        the flavor check only fires for a fleet that is itself privileged."""
        fake = FakeClient({"synthesizer": ("ok", "S")})
        run_argv = ["--fleet", _EXAMPLE_FLEET, "--task", "t", "--no-capture"]
        with patch.dict(os.environ, self._env()):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--approve-privileged"])
                with contextlib.redirect_stdout(io.StringIO()):
                    code = self.run_mod.main(run_argv)
        self.assertEqual(code, 0)


class TestSkillPreviewFlagsAPIBilledSynthesizer(unittest.TestCase):
    """--preview flags the example's Anthropic/Opus synthesizer with a cost warning."""

    def setUp(self):
        self.run_mod = _load_skill_module()

    def test_preview_flags_cost_warning_for_anthropic_opus(self):
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", MagicMock()):
            with patch.object(self.run_mod, "prepare_run_dir", MagicMock()):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview"])
        output = stdout_buf.getvalue()
        # The example synthesizer is openrouter/anthropic/claude-opus-4.8 —
        # must trigger the API-billing warning.
        self.assertIn("bills at API rates", output)


class TestSkillFleetRequired(unittest.TestCase):
    """--fleet is required; calling without it exits non-zero (argparse SystemExit(2))."""

    def setUp(self):
        self.run_mod = _load_skill_module()

    def test_missing_fleet_raises_system_exit(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_mod.main(["--task", "some task"])
        self.assertNotEqual(ctx.exception.code, 0)


class TestSkillTaskRequiredForRealRun(unittest.TestCase):
    """A real run (not preview) needs at least one of --task / --doc; with NEITHER
    present the run returns non-zero (exit 2) with no model calls — the check fires
    before ModelClient is ever constructed. These cases pass neither flag (the
    --doc-only-proceeds case lives in TestSkillDocFlag).

    Covers: the neither-present guard fires before capture (prepare_run_dir not called).
    """

    def setUp(self):
        self.run_mod = _load_skill_module()

    def test_missing_task_returns_nonzero(self):
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(stdout_buf):
                    code = self.run_mod.main(["--fleet", _EXAMPLE_FLEET])
        self.assertEqual(code, 2)

    def test_missing_task_makes_zero_model_calls(self):
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET])
        fake_client_cls.assert_not_called()

    def test_missing_task_emits_clear_message(self):
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET])
        self.assertIn("--task", stdout_buf.getvalue())

    def test_missing_task_prepare_run_dir_not_called(self):
        """Capture side-effect must NOT fire before the task-None guard."""
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        stdout_buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(stdout_buf):
                    self.run_mod.main(["--fleet", _EXAMPLE_FLEET])
        mock_prepare.assert_not_called()


class TestSkillFleetErrorPaths(unittest.TestCase):
    """Error-path tests for cadre/data/skill/run.py main(): nonexistent file,
    invalid YAML, and directory path all return 1 cleanly."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_mod = _load_skill_module()

    def _run_fleet(self, fleet_arg, extra_env=None):
        """Run skill main() with --no-capture, capturing stdout."""
        buf = io.StringIO()
        env = {"CADRE_RUN_DIR": str(self.tmp)}
        if extra_env:
            env.update(extra_env)
        with patch.dict(os.environ, env):
            with patch.object(self.run_mod, "ModelClient", MagicMock()):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(
                        ["--fleet", fleet_arg, "--task", "test", "--no-capture"]
                    )
        return code, buf.getvalue()

    def test_nonexistent_fleet_returns_1(self):
        """A --fleet path pointing to a nonexistent file returns exit code 1."""
        code, _out = self._run_fleet(str(self.tmp / "does_not_exist.yaml"))
        self.assertEqual(code, 1)

    def test_invalid_yaml_fleet_returns_1(self):
        """A --fleet path pointing to invalid YAML (ConfigError path) returns exit code 1."""
        bad = self.tmp / "bad.yaml"
        bad.write_text("name: [unterminated\n")
        code, _out = self._run_fleet(str(bad))
        self.assertEqual(code, 1)

    def test_directory_fleet_returns_1(self):
        """A --fleet path pointing at a directory (IsADirectoryError) returns exit code 1."""
        fleet_dir = self.tmp / "myfleets"
        fleet_dir.mkdir()
        code, _out = self._run_fleet(str(fleet_dir))
        self.assertEqual(code, 1)

    def test_directory_fleet_message_mentions_path(self):
        """The OSError message mentions the directory path that was passed."""
        fleet_dir = self.tmp / "myfleets2"
        fleet_dir.mkdir()
        _code, out = self._run_fleet(str(fleet_dir))
        self.assertIn(str(fleet_dir), out)

    def test_directory_fleet_no_traceback(self):
        """No Python traceback appears in the output — only a clean message."""
        fleet_dir = self.tmp / "myfleets3"
        fleet_dir.mkdir()
        _code, out = self._run_fleet(str(fleet_dir))
        self.assertNotIn("Traceback", out)


class TestSkillArbitraryFleet(unittest.TestCase):
    """Covers F2: any --fleet <path> runs, not just research-swarm."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_mod = _load_skill_module()

    def test_arbitrary_fleet_runs_and_writes_manifest(self):
        """A real run with --fleet <example> and a FakeClient exits 0, writes manifest."""
        fake = FakeClient({"synthesizer": ("ok", "SYNTH FROM FLEET")})
        run_dir = self.tmp
        run_argv = ["--fleet", _EXAMPLE_FLEET, "--task", "what is the best approach?"]
        env = {"CADRE_RUN_DIR": str(run_dir), "CADRE_APPROVAL_PATH": str(self.tmp / "approval")}
        with patch.dict(os.environ, env):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                # Mint the preview-bound approval for this exact surface first.
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                code = self.run_mod.main(run_argv)
        self.assertEqual(code, 0)
        self.assertTrue((run_dir / "manifest.json").exists())


# ---------------------------------------------------------------------------
# U4: live progress wiring — stream split (R9), edge events, full-folder integration
# ---------------------------------------------------------------------------


class TestRunCommandProgressStreamSplit(unittest.TestCase):
    """run_command puts the report on stdout (the returned output) and every [cadre]
    progress line on the injected progress_stream (stderr) — cleanly separable (R9)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_report_clean_of_progress_and_progress_has_breadcrumbs(self):
        # Covers AE1.
        prog = io.StringIO()
        client = FakeClient({"synthesizer": ("ok", "THE REPORT")})
        code, output = run_command(
            EXAMPLE, "find tools", client=client,
            run_dir=self.tmp / "r", progress_stream=prog,
        )
        self.assertEqual(code, 0)
        # Report (stdout) carries the synthesis and NO breadcrumbs.
        self.assertIn("THE REPORT", output)
        self.assertNotIn("[cadre]", output)
        # Progress (stderr) carries the breadcrumbs and NOT the synthesis body.
        prog_text = prog.getvalue()
        self.assertIn("[cadre] validated fleet", prog_text)
        self.assertIn("[cadre] lane ", prog_text)
        self.assertIn("[cadre] done in", prog_text)
        self.assertNotIn("THE REPORT", prog_text)

    def test_validated_and_run_folder_emitted_before_lanes(self):
        prog = io.StringIO()
        client = FakeClient({"synthesizer": ("ok", "S")})
        run_command(EXAMPLE, "task", client=client, run_dir=self.tmp / "r", progress_stream=prog)
        text = prog.getvalue()
        first_lane_at = text.index("[cadre] lane ")
        self.assertLess(text.index("validated fleet"), first_lane_at, "validated before any lane")
        self.assertLess(text.index("run folder:"), first_lane_at, "run folder before any lane")

    def test_completion_shows_total_and_run_folder(self):
        prog = io.StringIO()
        run_dir = self.tmp / "r"
        run_command(EXAMPLE, "task", client=FakeClient({"synthesizer": ("ok", "S")}),
                    run_dir=run_dir, progress_stream=prog)
        text = prog.getvalue()
        self.assertRegex(text, r"\[cadre\] done in \d+\.\d+s")
        self.assertIn(str(run_dir), text)  # completion names the run folder

    def test_no_capture_narrates_without_paths(self):
        # Covers AE5.
        prog = io.StringIO()
        code, _out = run_command(
            EXAMPLE, "task", client=FakeClient({"synthesizer": ("ok", "S")}),
            capture=False, progress_stream=prog,
        )
        self.assertEqual(code, 0)
        text = prog.getvalue()
        self.assertIn("[cadre] lane ", text)        # lanes still narrated
        self.assertNotIn("run folder", text)         # but no run-folder line (R13)
        self.assertNotIn("-> specialist-", text)     # and no per-lane artifact path


class TestRunCommandWritesCompleteFolder(unittest.TestCase):
    """U4 reconnects the per-lane writes U3 moved to the edge: a captured run now
    produces the COMPLETE folder — prompt.txt up front + per-lane files written
    incrementally + synthesis.md + manifest.json."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_full_folder_written(self):
        run_dir = self.tmp / "run"
        code, _out = run_command(
            EXAMPLE, "find best tools", client=FakeClient({"synthesizer": ("ok", "SYNTH")}),
            run_dir=run_dir, progress_stream=io.StringIO(),
        )
        self.assertEqual(code, 0)
        for fname in ("prompt.txt", "specialist-social.md", "specialist-web.md",
                      "specialist-analysis.md", "synthesis.md", "manifest.json"):
            self.assertTrue((run_dir / fname).exists(), f"missing artifact: {fname}")

    def test_prompt_txt_written_up_front_with_task(self):
        run_dir = self.tmp / "run2"
        run_command(EXAMPLE, "my exact task", client=FakeClient({"synthesizer": ("ok", "S")}),
                    run_dir=run_dir, progress_stream=io.StringIO())
        self.assertEqual((run_dir / "prompt.txt").read_text(encoding="utf-8"), "my exact task")

    def test_no_leaked_heartbeat_thread(self):
        run_dir = self.tmp / "run3"
        run_command(EXAMPLE, "task", client=FakeClient({"synthesizer": ("ok", "S")}),
                    run_dir=run_dir, progress_stream=io.StringIO())
        alive = [t for t in threading.enumerate() if t.name == "cadre-heartbeat" and t.is_alive()]
        self.assertEqual(alive, [], "heartbeat timer thread must be stopped after the run")


class _BrokenStream:
    """A progress stream whose write() always raises — models a closed/broken pipe
    (e.g. the supervising agent stopped draining the subprocess's stderr)."""

    def write(self, _s):
        raise BrokenPipeError("pipe closed")

    def flush(self):
        pass


class TestRunCommandStreamFailureResilience(unittest.TestCase):
    """A dead progress stream mid-run must NOT discard the synthesized report — the
    breadcrumbs are auxiliary, the FleetResult is the deliverable (R9)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_broken_progress_stream_still_returns_report(self):
        client = FakeClient({"synthesizer": ("ok", "THE REPORT")})
        code, output = run_command(
            EXAMPLE, "task", client=client,
            run_dir=self.tmp / "r", progress_stream=_BrokenStream(),
        )
        self.assertEqual(code, 0, "run must still succeed despite the dead progress pipe")
        self.assertIn("THE REPORT", output, "the synthesized report survives a broken stream")
        self.assertTrue((self.tmp / "r" / "manifest.json").exists(), "capture still completed")

    def test_save_run_failure_with_broken_warn_stream_still_returns_report(self):
        # The worst case: the supervising agent stopped draining the progress stream
        # (where the save_run warning now routes) AND save_run fails. The warning
        # print() must not raise out of run_command and cost the (already computed)
        # report.
        client = FakeClient({"synthesizer": ("ok", "MY SYNTHESIS")})
        with patch("cadre.cli.save_run", side_effect=OSError("disk full")):
            code, output = run_command(
                EXAMPLE, "task", client=client,
                run_dir=self.tmp / "r2", progress_stream=_BrokenStream(),
            )
        self.assertEqual(code, 0)
        self.assertIn("MY SYNTHESIS", output)

    def test_save_run_warning_routed_to_progress_stream_and_sanitized(self):
        # The warning rides the SAME [cadre] stream as the breadcrumbs (not sys.stderr),
        # and a control byte in the exception text (e.g. a run_dir path) can't forge a
        # second line.
        prog = io.StringIO()
        client = FakeClient({"synthesizer": ("ok", "S")})
        with patch("cadre.cli.save_run", side_effect=OSError("disk full\n[cadre] forged")):
            run_command(EXAMPLE, "task", client=client, run_dir=self.tmp / "r3", progress_stream=prog)
        text = prog.getvalue()
        self.assertIn("[cadre] warn: failed to save run artifacts:", text)
        warn_lines = [ln for ln in text.split("\n") if ln.startswith("[cadre] warn:")]
        self.assertEqual(len(warn_lines), 1, "warning must be a single sanitized line")
        self.assertNotIn("\n[cadre] forged", text)  # embedded newline stripped — no forged line


class TestRunCommandLaneCaptureFailure(unittest.TestCase):
    """A per-lane artifact write failure degrades (warns on the stream) and never
    crashes the run; other lanes still capture and the report is intact."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_save_lane_failure_warns_and_run_succeeds(self):
        from cadre.capture import save_lane as real_save_lane

        def flaky(lane, filename, run_dir):
            if lane.role == "web":
                raise OSError("disk full")
            return real_save_lane(lane, filename, run_dir)

        prog = io.StringIO()
        run_dir = self.tmp / "r"
        client = FakeClient({"synthesizer": ("ok", "SYNTH")})
        with patch("cadre.progress_runner.save_lane", flaky):
            code, output = run_command(
                EXAMPLE, "task", client=client, run_dir=run_dir, progress_stream=prog,
            )
        self.assertEqual(code, 0, "run succeeds despite a per-lane write failure")
        self.assertIn("SYNTH", output)
        self.assertFalse((run_dir / "specialist-web.md").exists(), "the failed lane's file is absent")
        self.assertTrue((run_dir / "specialist-social.md").exists(), "other lanes still captured")
        prog_text = prog.getvalue()
        self.assertIn("[cadre] warn:", prog_text, "the failure is warned on the [cadre] stream")
        self.assertIn("web", prog_text)


class TestLaneArtifactWrittenBeforeBreadcrumb(unittest.TestCase):
    """The 'lane … -> specialist-X.md' breadcrumb is emitted only AFTER the artifact
    is on disk, so a supervisor that parses the breadcrumb can rely on the file being
    there (Copilot review)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_artifact_exists_when_its_breadcrumb_is_emitted(self):
        run_dir = self.tmp / "r"
        violations = []

        class CheckingStream:
            def write(self, s):
                m = re.search(r"-> (specialist-[\w.-]+\.md)", s)
                if m and not (run_dir / m.group(1)).exists():
                    violations.append(s.strip())

            def flush(self):
                pass

        client = FakeClient({"synthesizer": ("ok", "S")})
        run_command(EXAMPLE, "task", client=client, run_dir=run_dir, progress_stream=CheckingStream())
        self.assertEqual(violations, [], f"breadcrumb named a not-yet-written artifact: {violations}")


class TestSkillProgressStreamSplit(unittest.TestCase):
    """The skill entry (run.py) puts the report on stdout and [cadre] progress on
    stderr, and writes the complete run folder — via the by-path skill loader."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_mod = _load_skill_module()

    def test_report_stdout_progress_stderr_and_full_folder(self):
        fake = FakeClient({"synthesizer": ("ok", "SKILL REPORT")})
        out_buf, err_buf = io.StringIO(), io.StringIO()
        run_argv = ["--fleet", _EXAMPLE_FLEET, "--task", "what is best?"]
        env = {"CADRE_RUN_DIR": str(self.tmp), "CADRE_APPROVAL_PATH": str(self.tmp / "approval")}
        with patch.dict(os.environ, env):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                # Mint the preview-bound approval for this exact surface first.
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    code = self.run_mod.main(run_argv)
        self.assertEqual(code, 0)
        # Report on stdout, breadcrumbs on stderr — cleanly separated.
        self.assertIn("SKILL REPORT", out_buf.getvalue())
        self.assertNotIn("[cadre]", out_buf.getvalue())
        self.assertIn("[cadre]", err_buf.getvalue())
        self.assertNotIn("SKILL REPORT", err_buf.getvalue())
        # Complete folder written (prompt up front + per-lane + synth + manifest).
        for fname in ("prompt.txt", "specialist-web.md", "synthesis.md", "manifest.json"):
            self.assertTrue((self.tmp / fname).exists(), f"missing: {fname}")


# ---------------------------------------------------------------------------
# U4 (fleet-shapes): collect fleet validate + exit-code tests
# ---------------------------------------------------------------------------

_COLLECT_FLEET_YAML = """\
name: test-collect
convergence: collect
specialists:
  - role: web
    provider: openrouter
    model: google/gemini-3-flash
    toolset: [web]
    focus: web research
  - role: social
    provider: xai
    model: grok-4.3
    toolset: [x_search]
    focus: social scan
"""


class TestValidateCollectFleet(unittest.TestCase):
    """validate_command on a collect fleet (no synthesis) succeeds and mentions collect."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(_COLLECT_FLEET_YAML)
        self.addCleanup(os.unlink, self.path)

    def test_validate_collect_fleet_returns_zero(self):
        """validate_command exits 0 for a valid collect fleet."""
        code, _out = validate_command(self.path)
        self.assertEqual(code, 0)

    def test_validate_collect_fleet_no_crash(self):
        """validate_command does not crash when cfg.synthesis is None (collect fleet)."""
        # If this raises AttributeError on cfg.synthesis.provider, the test fails.
        code, out = validate_command(self.path)
        self.assertEqual(code, 0)
        self.assertIn("test-collect", out)

    def test_validate_collect_fleet_mentions_collect_in_output(self):
        """validate_command output mentions 'collect' for a collect fleet."""
        _code, out = validate_command(self.path)
        self.assertIn("collect", out)

    def test_validate_collect_fleet_does_not_crash_on_synthesis_access(self):
        """Regression: line 36 cfg.synthesis.provider must not AttributeError for collect."""
        try:
            validate_command(self.path)
        except AttributeError as exc:
            self.fail(f"validate_command raised AttributeError on collect fleet: {exc}")


class TestRunCommandCollectExitCodes(unittest.TestCase):
    """Collect run: >=1 success → exit 0; all-failed → exit 1."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        fd, self.fleet_path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(_COLLECT_FLEET_YAML)
        self.addCleanup(os.unlink, self.fleet_path)

    def test_collect_success_exits_zero(self):
        """A collect run with >=1 specialist success exits 0 and renders the collect shape."""
        # FakeClient defaults to ("ok", ...) for every role — both specialists succeed.
        client = FakeClient()
        code, out = run_command(self.fleet_path, "find tools", client=client, capture=False)
        self.assertEqual(code, 0)
        # Pin the integration render path: a successful collect run must NOT be framed as a
        # failure (the load-bearing ok-aliasing — a regression routing it to the
        # "partial result (no synthesis)" branch would be caught here, not just the exit code).
        self.assertIn("collect result", out)
        self.assertNotIn("synthesis was not attempted", out)

    def test_collect_all_failed_exits_nonzero(self):
        """A collect run with all specialists failed exits 1."""
        client = FakeClient({r: ("fail", "down") for r in ("web", "social")})
        code, _out = run_command(self.fleet_path, "find tools", client=client, capture=False)
        self.assertEqual(code, 1)


# ---------------------------------------------------------------------------
# U6 (judge convergence): validate display + run exit-code tests
# ---------------------------------------------------------------------------

_JUDGE_FLEET_YAML = """\
name: test-judge
convergence: judge
judge:
  provider: openrouter
  model: anthropic/claude-opus-4.8
  prompt: Grade each specialist output.
specialists:
  - role: web
    provider: openrouter
    model: google/gemini-3-flash
    toolset: []
    focus: web research
  - role: social
    provider: xai
    model: grok-4.3
    toolset: []
    focus: social scan
"""


class TestValidateJudgeFleet(unittest.TestCase):
    """validate_command on a judge fleet (synthesis=None) succeeds and shows judge info."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(_JUDGE_FLEET_YAML)
        self.addCleanup(os.unlink, self.path)

    def test_validate_judge_fleet_returns_zero(self):
        """validate exits 0 for a valid judge fleet."""
        code, _out = validate_command(self.path)
        self.assertEqual(code, 0)

    def test_validate_judge_fleet_no_attribute_error(self):
        """validate_command must not raise AttributeError on cfg.synthesis (None in judge mode)."""
        try:
            validate_command(self.path)
        except AttributeError as exc:
            self.fail(f"validate_command raised AttributeError on judge fleet: {exc}")

    def test_validate_judge_fleet_shows_convergence_judge(self):
        """validate output includes the 'convergence: judge' line."""
        _code, out = validate_command(self.path)
        self.assertIn("convergence: judge", out)

    def test_validate_judge_fleet_shows_judge_model(self):
        """validate output includes the judge provider/model string."""
        _code, out = validate_command(self.path)
        self.assertIn("judge: openrouter/anthropic/claude-opus-4.8", out)

    def test_validate_judge_fleet_no_synthesis_line(self):
        """validate output for a judge fleet must not emit a bare 'synthesis:' line."""
        _code, out = validate_command(self.path)
        for line in out.splitlines():
            self.assertFalse(
                line.strip().startswith("synthesis:"),
                f"unexpected synthesis line in judge fleet output: {line!r}",
            )


class TestRunCommandJudgeExitCodes(unittest.TestCase):
    """Judge run exit codes: success→0; partial coverage→0; judge-fail→1; all-fail→1."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        fd, self.fleet_path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(_JUDGE_FLEET_YAML)
        self.addCleanup(os.unlink, self.fleet_path)

    def test_judge_success_exits_zero(self):
        """All specialists + judge succeed → exit 0."""
        client = FakeClient({"judge": ("ok", "GRADE TEXT")})
        code, _out = run_command(self.fleet_path, "task", client=client, capture=False)
        self.assertEqual(code, 0)

    def test_judge_failure_exits_nonzero(self):
        """Judge ran but returned an error → exit 1."""
        client = FakeClient({"judge": ("fail", "rate limited")})
        code, _out = run_command(self.fleet_path, "task", client=client, capture=False)
        self.assertEqual(code, 1)

    def test_all_specialists_failed_exits_nonzero(self):
        """All specialists failed (judge never ran) → exit 1."""
        client = FakeClient({r: ("fail", "down") for r in ("web", "social")})
        code, _out = run_command(self.fleet_path, "task", client=client, capture=False)
        self.assertEqual(code, 1)

    def test_low_grade_still_exits_zero(self):
        """Judge call succeeded (ok=True) with a low/critical grade text → exit 0.

        The grade is advisory (R3/KTD5) — grade content never gates the exit code;
        a low-scored-but-successful judge run is ordinary success.
        """
        client = FakeClient({"judge": ("ok", "Grade: F - All lanes failed critically.")})
        code, _out = run_command(self.fleet_path, "task", client=client, capture=False)
        self.assertEqual(code, 0)

    def test_partial_coverage_exits_zero_manifest_has_ungraded(self):
        """Judge succeeded but some lanes were not in the grade text → exit 0;
        manifest.ungraded carries the ungraded lanes (partial signal in the artifact,
        not the exit code — KTD5)."""
        import json
        # Judge text grades only 'web'; 'social' gets no structured block → ungraded.
        partial_grade = (
            "=== LANE: web ===\n"
            "Grade: A\n"
            "Rationale: Good work.\n"
        )
        run_dir = self.tmp / "partial"
        client = FakeClient({"judge": ("ok", partial_grade)})
        code, _out = run_command(self.fleet_path, "task", client=client, run_dir=run_dir)
        self.assertEqual(code, 0, "partial coverage must still exit 0")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        ungraded = manifest.get("ungraded", [])
        self.assertTrue(
            any(u["role"] == "social" for u in ungraded),
            f"manifest must list 'social' as ungraded; got: {ungraded}",
        )


class _PromptCapturingClient(FakeClient):
    """FakeClient that also records the prompt each specialist received."""

    def __init__(self, behavior=None):
        super().__init__(behavior)
        self.prompts = {}

    def run(self, *, role, provider, model, prompt, toolset=()):
        self.prompts[role] = prompt
        return super().run(role=role, provider=provider, model=model, prompt=prompt, toolset=toolset)


class TestPersonaResolutionViaCLI(unittest.TestCase):
    """The CLI validate/run path resolves personas via default_pool_dir() (KTD8).

    Guards the CLI<->resolver wiring. Every other CLI test uses focus-only fleets,
    so resolve() short-circuits with zero I/O and these call sites are otherwise
    unexercised — deleting resolve() from validate_command/run_command would stay
    green without this. The pool is redirected via CADRE_PERSONAS_DIR (the test seam).
    """

    def setUp(self):
        self._pool = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._pool)
        self._body = "You are a reviewer. Cite the exact passage. Say 'no finding' if sound.\n"
        with open(os.path.join(self._pool, "rev.md"), "w", encoding="utf-8") as f:
            f.write(self._body)
        self._fleet = _tmp_yaml(
            "name: p\n"
            "convergence: collect\n"
            "specialists:\n"
            "  - {role: r, provider: openrouter, model: m, toolset: [], persona: rev}\n"
        )
        self.addCleanup(os.unlink, self._fleet)

    def test_validate_resolves_persona_against_pool(self):
        """validate exits 0 for a persona fleet whose persona resolves in the pool."""
        with patch.dict(os.environ, {"CADRE_PERSONAS_DIR": self._pool}):
            code, out = validate_command(self._fleet)
        self.assertEqual(code, 0, f"persona fleet should validate cleanly; got: {out}")

    def test_validate_missing_persona_errors(self):
        """A persona absent from the pool fails validate — proves validate resolves."""
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        with patch.dict(os.environ, {"CADRE_PERSONAS_DIR": empty}):
            code, out = validate_command(self._fleet)
        self.assertEqual(code, 1, "a persona absent from the pool must fail validate")
        self.assertIn("rev", out)

    def test_run_builds_prompt_from_persona(self):
        """run resolves the persona and the body reaches the specialist prompt end-to-end."""
        client = _PromptCapturingClient()
        with patch.dict(os.environ, {"CADRE_PERSONAS_DIR": self._pool}):
            code, out = run_command(self._fleet, "review this", client=client, capture=False)
        self.assertEqual(code, 0, f"persona fleet should run; got: {out}")
        self.assertIn(
            "Cite the exact passage.",
            client.prompts.get("r", ""),
            "the resolved persona body must reach the specialist prompt",
        )


class TestCLIDocFlag(unittest.TestCase):
    """cadre/cli.py `run --doc`: read named files into the task (#26).

    Drives cli.main() so the argparse wiring (--doc; --task now optional) and the
    at-least-one guard are exercised end-to-end. A prompt-capturing client (via a
    patched ModelClient) proves the composed content reaches the specialist prompt.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _doc(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def _run(self, argv, fake=None):
        fake = fake if fake is not None else _PromptCapturingClient()
        buf = io.StringIO()
        with patch("cadre.cli.ModelClient", return_value=fake):
            with contextlib.redirect_stdout(buf):
                code = cli_main(argv)
        return code, fake, buf.getvalue()

    def test_doc_content_reaches_prompt(self):
        """Covers AE1: run spec --doc f.md → file content reaches the prompt."""
        doc = self._doc("spec.md", "SENTINEL_SPEC_CONTENT")
        code, fake, _out = self._run(["run", EXAMPLE, "--doc", doc, "--no-capture"])
        self.assertEqual(code, 0)
        self.assertIn("SENTINEL_SPEC_CONTENT", fake.prompts["web"])

    def test_task_only_passes_verbatim(self):
        """Covers AE6/R3: --task with no --doc is unchanged — no file blocks."""
        code, fake, _out = self._run(["run", EXAMPLE, "--task", "PLAIN_TASK", "--no-capture"])
        self.assertEqual(code, 0)
        self.assertIn("PLAIN_TASK", fake.prompts["web"])
        self.assertNotIn("=== FILE:", fake.prompts["web"])

    def test_doc_only_no_task_composes(self):
        """--doc only (no --task) composes the task from the block and runs."""
        doc = self._doc("only.md", "ONLY_SPEC_BODY")
        code, fake, _out = self._run(["run", EXAMPLE, "--doc", doc, "--no-capture"])
        self.assertEqual(code, 0)
        self.assertIn("ONLY_SPEC_BODY", fake.prompts["web"])

    def test_missing_doc_clean_nonzero_naming_path(self):
        """Covers AE2/R5: an unreadable --doc fails cleanly (exit 1), naming the path,
        before any model call."""
        missing = str(self.tmp / "ghost.md")
        code, fake, out = self._run(["run", EXAMPLE, "--task", "t", "--doc", missing, "--no-capture"])
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.calls), 0, "no model call when a --doc is unreadable")
        self.assertIn(missing, out)
        self.assertNotIn("Traceback", out)

    def test_neither_task_nor_doc_exits_2(self):
        """Neither --task nor --doc → exit 2 from the at-least-one check (not argparse)."""
        code, fake, out = self._run(["run", EXAMPLE, "--no-capture"])
        self.assertEqual(code, 2)
        self.assertEqual(len(fake.calls), 0)
        self.assertIn("--doc", out)

    def test_oversize_doc_run_warns_on_stderr(self):
        """A non-preview run with an oversize --doc warns the operator on stderr that
        the review runs over a partial file (cli has no preview to disclose it)."""
        from cadre.file_input import MAX_FILE_BYTES
        big = self._doc("huge.md", "x" * (MAX_FILE_BYTES + 4096))
        fake = _PromptCapturingClient()
        err = io.StringIO()
        with patch("cadre.cli.ModelClient", return_value=fake):
            with contextlib.redirect_stderr(err):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli_main(["run", EXAMPLE, "--doc", big, "--no-capture"])
        self.assertEqual(code, 0)
        self.assertIn("[cadre] warn:", err.getvalue())
        self.assertIn("truncated", err.getvalue())

    def test_capture_writes_composed_task_to_prompt_txt(self):
        """With capture ON, prompt.txt records the COMPOSED task (the file block), not
        the raw args.task — the run folder must reflect what specialists actually saw."""
        doc = self._doc("plan.md", "CAPTURED_DOC_BODY")
        run_dir = self.tmp / "run"
        fake = _PromptCapturingClient()
        with patch("cadre.cli.ModelClient", return_value=fake):
            with patch.dict(os.environ, {"CADRE_RUN_DIR": str(run_dir)}):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli_main(["run", EXAMPLE, "--task", "review", "--doc", doc])
        self.assertEqual(code, 0)
        prompt_txt = (run_dir / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("CAPTURED_DOC_BODY", prompt_txt)
        self.assertIn("=== FILE:", prompt_txt)

    def test_validate_subcommand_rejects_doc_flag(self):
        """validate is config-only and out of scope — argparse must reject --doc there."""
        with self.assertRaises(SystemExit) as ctx:
            cli_main(["validate", EXAMPLE, "--doc", "x.md"])
        self.assertNotEqual(ctx.exception.code, 0)


class TestSkillDocFlag(unittest.TestCase):
    """cadre/data/skill/run.py --doc: read named files into the task (#26).

    Covers AE1/AE2/AE4/AE6 + R2/R3/R5/R7 at the skill entry. A prompt-capturing
    client proves the composed file content actually reaches the specialist prompt.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_mod = _load_skill_module()

    def _doc(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def _run(self, argv, fake=None):
        """Run skill main() with a prompt-capturing client; returns (code, fake, stdout).

        Mints a matching preview-bound approval first (same --fleet/--task/--doc
        argv, same CADRE_APPROVAL_PATH) so the real run's gate (U4) sees a valid
        token. Harmless for the callers whose argv never reaches the gate (no
        --task/--doc, or an unreadable --doc): the mint call mints nothing / fails
        the same way, before ever touching approval.
        """
        fake = fake if fake is not None else _PromptCapturingClient()
        buf = io.StringIO()
        run_argv = ["--fleet", _EXAMPLE_FLEET] + argv
        env = {"CADRE_APPROVAL_PATH": str(self.tmp / "approval")}
        with patch.dict(os.environ, env):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(run_argv)
        return code, fake, buf.getvalue()

    def test_doc_content_reaches_prompt(self):
        """Covers AE1: --doc file content reaches the specialist prompt."""
        doc = self._doc("plan.md", "SENTINEL_PLAN_CONTENT")
        code, fake, _out = self._run(["--task", "review", "--doc", doc, "--no-capture"])
        self.assertEqual(code, 0)
        self.assertIn("SENTINEL_PLAN_CONTENT", fake.prompts["web"])

    def test_two_docs_reach_prompt_in_order(self):
        """Covers R2: two --doc blocks reach the prompt in flag order."""
        a = self._doc("a.md", "ALPHA_DOC")
        b = self._doc("b.md", "BETA_DOC")
        code, fake, _out = self._run(["--task", "t", "--doc", a, "--doc", b, "--no-capture"])
        self.assertEqual(code, 0)
        prompt = fake.prompts["web"]
        self.assertIn("ALPHA_DOC", prompt)
        self.assertIn("BETA_DOC", prompt)
        self.assertLess(prompt.index("ALPHA_DOC"), prompt.index("BETA_DOC"))

    def test_task_only_passes_verbatim(self):
        """Covers AE6/R3: no --doc means no file blocks — the task is unchanged."""
        code, fake, _out = self._run(["--task", "PLAIN_TASK", "--no-capture"])
        self.assertEqual(code, 0)
        prompt = fake.prompts["web"]
        self.assertIn("PLAIN_TASK", prompt)
        self.assertNotIn("=== FILE:", prompt)

    def test_doc_only_no_task_proceeds(self):
        """--doc only (no --task) composes from the block and the run proceeds."""
        doc = self._doc("only.md", "ONLY_DOC_CONTENT")
        code, fake, _out = self._run(["--doc", doc, "--no-capture"])
        self.assertEqual(code, 0)
        self.assertIn("ONLY_DOC_CONTENT", fake.prompts["web"])

    def test_neither_task_nor_doc_exits_2_no_model_call(self):
        """Neither --task nor --doc → clean usage error (exit 2), zero model calls."""
        code, fake, out = self._run(["--no-capture"])
        self.assertEqual(code, 2)
        self.assertEqual(len(fake.calls), 0)
        self.assertIn("--task", out)
        self.assertIn("--doc", out)

    def test_missing_doc_clean_nonzero_no_model_call(self):
        """Covers AE2/R5: an unreadable --doc fails the run cleanly, naming the path,
        before any model call (no traceback)."""
        missing = str(self.tmp / "ghost.md")
        code, fake, out = self._run(["--task", "t", "--doc", missing, "--no-capture"])
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.calls), 0, "no model call when a --doc is unreadable")
        self.assertIn(missing, out)
        self.assertNotIn("Traceback", out)

    def test_preview_with_doc_shows_path_and_makes_no_model_call(self):
        """Covers AE4 + R7: --preview --doc shows the --doc path AND makes zero
        model calls / zero capture side-effects."""
        doc = self._doc("preview.md", "PREVIEW_DOC_BODY")
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview", "--doc", doc])
        self.assertEqual(code, 0)
        self.assertIn(doc, buf.getvalue(), "the --doc path appears in the preview (R7)")
        fake_client_cls.assert_not_called()
        mock_prepare.assert_not_called()

    def test_preview_missing_doc_fails_read_check_no_model_call(self):
        """Covers R5/KTD7: the preview doubles as a read-check — a missing --doc fails
        the preview with the path-naming error, before approval, zero model calls."""
        missing = str(self.tmp / "absent.md")
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview", "--doc", missing])
        self.assertEqual(code, 1)
        self.assertIn(missing, buf.getvalue())
        fake_client_cls.assert_not_called()
        mock_prepare.assert_not_called()

    def test_oversize_doc_run_warns_on_stderr(self):
        """A non-preview skill run with an oversize --doc warns on stderr (run path has
        no preview gate to disclose the partial-file truncation)."""
        from cadre.file_input import MAX_FILE_BYTES
        big = self._doc("huge.md", "x" * (MAX_FILE_BYTES + 4096))
        run_argv = ["--fleet", _EXAMPLE_FLEET, "--doc", big, "--no-capture"]
        fake = _PromptCapturingClient()
        err = io.StringIO()
        env = {"CADRE_APPROVAL_PATH": str(self.tmp / "approval")}
        with patch.dict(os.environ, env):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                # Mint the preview-bound approval for this exact surface first.
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_mod.main(run_argv + ["--preview"])
                with contextlib.redirect_stderr(err):
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = self.run_mod.main(run_argv)
        self.assertEqual(code, 0)
        self.assertIn("[cadre] warn:", err.getvalue())
        self.assertIn("truncated", err.getvalue())

    def test_preview_marks_oversize_doc_as_truncated(self):
        """The preview discloses that an oversize --doc will be reviewed only partially.
        The in-block truncation note is model-facing; the human-approval surface must
        surface it too (cross-model review finding), still with zero model calls."""
        from cadre.file_input import MAX_FILE_BYTES
        big = self.tmp / "huge.md"
        big.write_text("x" * (MAX_FILE_BYTES + 4096), encoding="utf-8")
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview", "--doc", str(big)])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(str(big), out)
        self.assertIn("truncated", out, "the preview must disclose the partial-file truncation")
        fake_client_cls.assert_not_called()
        mock_prepare.assert_not_called()

    def test_preview_non_utf8_doc_fails_read_check_no_model_call(self):
        """The preview read-check rejects a non-UTF-8 --doc too (not just a missing one):
        compose runs before the preview branch, so every failure mode is caught early."""
        binf = self.tmp / "image.bin"
        binf.write_bytes(b"\xff\xfe\x00\x80 not text")
        fake_client_cls = MagicMock()
        mock_prepare = MagicMock()
        buf = io.StringIO()
        with patch.object(self.run_mod, "ModelClient", fake_client_cls):
            with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
                with contextlib.redirect_stdout(buf):
                    code = self.run_mod.main(["--fleet", _EXAMPLE_FLEET, "--preview", "--doc", str(binf)])
        self.assertEqual(code, 1)
        self.assertIn(str(binf), buf.getvalue())
        fake_client_cls.assert_not_called()
        mock_prepare.assert_not_called()


# ---------------------------------------------------------------------------
# U4: `cadre setup` — provisions ~/.cadre from the installed package, KTD11
# fail-closed on a recorded Python that can't `import cadre`.
# (docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md)
# ---------------------------------------------------------------------------


def _make_stub_python(test_case, exit_code):
    """Write a tiny always-<exit_code> executable; return its path.

    A shell stub (not a real python) simulates the OUTCOME of "a recorded
    interpreter that can/can't import cadre" without depending on cwd/sys.path
    subtlety around what a real broken venv would resolve to on this machine —
    see cadre.provision.verify_importable's own docstring: the real subprocess
    check runs with `-P` (cwd is never on sys.path there), so a shell stub's
    exit code is a clean, direct stand-in for the check's True/False outcome
    regardless of args passed to it.
    """
    tmp = tempfile.mkdtemp()
    test_case.addCleanup(shutil.rmtree, tmp)
    stub = Path(tmp) / "fake-python"
    stub.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    stub.chmod(0o700)
    return str(stub)


class TestSetupCommandCleanHome(unittest.TestCase):
    """cadre setup against a clean tmp $HOME seeds everything from package data.

    These tests are about what happens AFTER the KTD11 gate passes (seeding,
    config-writing) — not about the gate itself (that's
    TestSetupCommandKTD11FailClosed, using deterministic stub pythons). Before
    Fix A, `setup_command(None)` recorded sys.executable and the real gate
    happened to pass for it via the cwd fail-open (this dev venv does not
    actually have cadre pip-installed into its own site-packages). `-P` closes
    that fail-open, so these tests now mock the gate directly rather than
    depend on incidental cwd behavior — sys.executable is still what gets
    RECORDED (untouched), only whether the gate accepts it is mocked.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.verify_patch = patch("cadre.provision.verify_importable", return_value=True)
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def test_seeds_fleets_personas_palette_and_config(self):
        code, out = setup_command(None)
        self.assertEqual(code, 0, out)
        cadre_home = Path(self.tmp) / ".cadre"
        fleets = list((cadre_home / "fleets").glob("*.yaml"))
        personas = list((cadre_home / "personas").glob("*.md"))
        self.assertEqual(len(fleets), 7, f"expected 7 starter fleets, got {[f.name for f in fleets]}")
        self.assertEqual(len(personas), 5, f"expected 5 personas, got {[p.name for p in personas]}")
        self.assertTrue((cadre_home / "palette-candidates.yaml").exists())
        self.assertTrue((cadre_home / "config").exists())

    def test_config_line_is_exact_and_records_sys_executable(self):
        code, out = setup_command(None)
        self.assertEqual(code, 0, out)
        config_path = Path(self.tmp) / ".cadre" / "config"
        content = config_path.read_text(encoding="utf-8")
        self.assertEqual(content, f"CADRE_HERMES_PYTHON={sys.executable}\n")

    def test_returns_zero_and_confirms_home_in_message(self):
        code, out = setup_command(None)
        self.assertEqual(code, 0)
        self.assertIn(str(Path(self.tmp) / ".cadre"), out)


class TestSetupCommandKTD11FailClosed(unittest.TestCase):
    """A recorded Python that cannot `import cadre` fails setup closed — nothing written."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_non_importing_python_exits_nonzero(self):
        stub = _make_stub_python(self, 1)
        code, out = setup_command(stub)
        self.assertNotEqual(code, 0)
        self.assertIn(stub, out)

    def test_non_importing_python_writes_no_config(self):
        stub = _make_stub_python(self, 1)
        setup_command(stub)
        config_path = Path(self.tmp) / ".cadre" / "config"
        self.assertFalse(config_path.exists())

    def test_non_importing_python_seeds_nothing_not_even_the_home_dir(self):
        """The import-check runs BEFORE ensure_cadre_dirs — fail-fast with nothing
        partial (KTD11): not a single directory gets created."""
        stub = _make_stub_python(self, 1)
        setup_command(stub)
        cadre_home = Path(self.tmp) / ".cadre"
        self.assertFalse(cadre_home.exists())

    def test_importing_python_succeeds(self):
        """The mirror case: a stub that DOES exit 0 (simulating a python that can
        import cadre) is accepted and recorded verbatim."""
        stub = _make_stub_python(self, 0)
        code, out = setup_command(stub)
        self.assertEqual(code, 0, out)
        config_path = Path(self.tmp) / ".cadre" / "config"
        self.assertEqual(config_path.read_text(encoding="utf-8"), f"CADRE_HERMES_PYTHON={stub}\n")


class TestSetupCommandNewlineRejection(unittest.TestCase):
    """C4: a recorded_python containing a newline/CR/NUL is rejected BEFORE
    verify_importable ever runs. Closes a real config-contract break: the
    skill's `grep -E '^CADRE_HERMES_PYTHON=' | cut -d= -f2-` reader only sees
    the FIRST line of the value, so a smuggled trailing line after a newline
    could pass verify_importable's real execve-based check (the full string)
    yet have the skill later run a different, unverified path.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_newline_rejected_before_verify_importable_runs(self):
        with patch("cadre.provision.verify_importable") as mock_verify:
            code, out = setup_command("/tmp/py\nsuffix")
        mock_verify.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("newline", out.lower())

    def test_newline_writes_no_config(self):
        with patch("cadre.provision.verify_importable"):
            setup_command("/tmp/py\nsuffix")
        config_path = Path(self.tmp) / ".cadre" / "config"
        self.assertFalse(config_path.exists())

    def test_carriage_return_rejected(self):
        with patch("cadre.provision.verify_importable") as mock_verify:
            code, out = setup_command("/tmp/py\rsuffix")
        mock_verify.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("control character", out.lower())

    def test_nul_byte_rejected(self):
        with patch("cadre.provision.verify_importable") as mock_verify:
            code, out = setup_command("/tmp/py\x00suffix")
        mock_verify.assert_not_called()
        self.assertEqual(code, 1)

    def test_newline_via_env_var_also_rejected(self):
        """The check applies regardless of which precedence branch (arg vs
        CADRE_HERMES_PYTHON env) produced recorded_python."""
        with patch("cadre.provision.verify_importable") as mock_verify:
            code, out = setup_command(None, env={"CADRE_HERMES_PYTHON": "/tmp/py\nsuffix"})
        mock_verify.assert_not_called()
        self.assertEqual(code, 1)


class TestSetupCommandBareNameResolution(unittest.TestCase):
    """C3: a bare/relative --venv-python is PATH-unstable (verify-time PATH
    here vs skill-run-time PATH later can resolve two different binaries) —
    setup_command resolves it to an absolute path via shutil.which before
    verifying and recording, so the value verified is the value later run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.verify_patch = patch("cadre.provision.verify_importable", return_value=True)
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def test_bare_name_resolved_verified_and_recorded_as_absolute_path(self):
        with patch("shutil.which", return_value="/abs/path/to/python") as mock_which:
            with patch("cadre.provision.verify_importable", return_value=True) as mock_verify:
                code, out = setup_command("python")
        mock_which.assert_called_once_with("python")
        mock_verify.assert_called_once_with("/abs/path/to/python")
        self.assertEqual(code, 0, out)
        config_path = Path(self.tmp) / ".cadre" / "config"
        self.assertEqual(
            config_path.read_text(encoding="utf-8"),
            "CADRE_HERMES_PYTHON=/abs/path/to/python\n",
        )

    def test_unresolvable_bare_name_left_unchanged(self):
        """shutil.which returning None (not found on PATH) leaves the bare
        name as-is: verify_importable then fails closed on it in real usage
        (mocked True here, since this test is about what gets RECORDED)."""
        with patch("shutil.which", return_value=None):
            code, out = setup_command("totally-bogus-not-on-path")
        self.assertEqual(code, 0, out)
        config_path = Path(self.tmp) / ".cadre" / "config"
        self.assertEqual(
            config_path.read_text(encoding="utf-8"),
            "CADRE_HERMES_PYTHON=totally-bogus-not-on-path\n",
        )

    def test_relative_path_with_slash_recorded_as_absolute(self):
        """Codex confirm: shutil.which returns a relative path WITH a directory
        component (e.g. '.venv/bin/python') unchanged, so it must be abspath'd
        against the current cwd before recording — otherwise the skill later
        runs a cwd-relative interpreter that differs from the verified one."""
        rel = os.path.join(".venv", "bin", "python")
        with patch("shutil.which", return_value=rel):
            code, out = setup_command(rel)
        self.assertEqual(code, 0, out)
        recorded = (Path(self.tmp) / ".cadre" / "config").read_text(encoding="utf-8")
        self.assertEqual(recorded, f"CADRE_HERMES_PYTHON={os.path.abspath(rel)}\n")
        self.assertTrue(os.path.isabs(recorded.split("=", 1)[1].strip()))

    def test_absolute_path_not_passed_through_which(self):
        """An already-absolute path (e.g. sys.executable) is untouched — no
        shutil.which call at all."""
        with patch("shutil.which") as mock_which:
            code, out = setup_command(sys.executable)
        mock_which.assert_not_called()
        self.assertEqual(code, 0, out)


class TestSetupCommandHomeSafetyGuard(unittest.TestCase):
    """C5a: a pre-planted symlinked or group/other-writable ~/.cadre is
    refused BEFORE any write — the provisioning steps below would otherwise
    write fleets/config/palette THROUGH a tampered control directory (no MAC
    protects ~/.cadre's contents; its integrity rests on this)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.verify_patch = patch("cadre.provision.verify_importable", return_value=True)
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def test_symlinked_cadre_home_refused(self):
        elsewhere = Path(self.tmp) / "attacker-target"
        elsewhere.mkdir(mode=0o700)
        cadre_home = Path(self.tmp) / ".cadre"
        cadre_home.symlink_to(elsewhere, target_is_directory=True)

        code, out = setup_command(None)

        self.assertEqual(code, 1)
        self.assertIn("symlink", out.lower())
        self.assertEqual(list(elsewhere.iterdir()), [], "nothing written through the symlinked home")
        self.assertTrue(cadre_home.is_symlink(), "the symlink itself must be untouched, not replaced")

    def test_group_writable_cadre_home_refused(self):
        cadre_home = Path(self.tmp) / ".cadre"
        cadre_home.mkdir(mode=0o770)
        os.chmod(cadre_home, 0o770)  # chmod after mkdir — mkdir's mode arg is umask-affected

        code, out = setup_command(None)

        self.assertEqual(code, 1)
        self.assertIn("writable", out.lower())
        self.assertFalse((cadre_home / "config").exists())

    def test_other_writable_cadre_home_refused(self):
        cadre_home = Path(self.tmp) / ".cadre"
        cadre_home.mkdir(mode=0o707)
        os.chmod(cadre_home, 0o707)

        code, out = setup_command(None)

        self.assertEqual(code, 1)
        self.assertIn("writable", out.lower())

    def test_preexisting_0o700_cadre_home_accepted(self):
        """A normal owner-only ~/.cadre (e.g. from a prior successful setup
        run) must NOT be refused — only a symlink or a loose mode is unsafe."""
        cadre_home = Path(self.tmp) / ".cadre"
        cadre_home.mkdir(mode=0o700)
        os.chmod(cadre_home, 0o700)

        code, out = setup_command(None)

        self.assertEqual(code, 0, out)

    def test_no_preexisting_cadre_home_accepted(self):
        """The common case: ~/.cadre does not exist yet — nothing to refuse."""
        code, out = setup_command(None)
        self.assertEqual(code, 0, out)


class TestSetupCommandOSErrorDegrade(unittest.TestCase):
    """Fix C regression: an OSError from the scaffold/seed/write-config sequence
    degrades to a clean (1, message) — never a raw traceback (the pre-#11
    scripts/resolve_venv.py wrapped this identical sequence the same way)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_ensure_cadre_dirs_oserror_degrades_cleanly(self):
        """An mkdir failure (e.g. a read-only $HOME, a full disk) inside
        ensure_cadre_dirs() must not propagate past setup_command."""
        with patch("cadre.provision.verify_importable", return_value=True):
            with patch("cadre.provision.ensure_cadre_dirs", side_effect=OSError("disk full")):
                try:
                    code, out = setup_command(None)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"setup_command raised instead of degrading cleanly: {exc!r}")
        self.assertEqual(code, 1)
        self.assertIn("disk full", out)

    def test_write_config_oserror_degrades_cleanly(self):
        """A write failure inside write_config() (the last step) must also
        degrade cleanly, not just an early-sequence failure."""
        with patch("cadre.provision.verify_importable", return_value=True):
            with patch("cadre.provision.write_config", side_effect=OSError("permission denied")):
                try:
                    code, out = setup_command(None)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"setup_command raised instead of degrading cleanly: {exc!r}")
        self.assertEqual(code, 1)
        self.assertIn("permission denied", out)


class TestSetupCommandPythonPrecedence(unittest.TestCase):
    """--venv-python arg > CADRE_HERMES_PYTHON env > sys.executable (KTD11).

    These tests are about precedence (which path gets RECORDED), not the
    KTD11 gate itself, so the gate is mocked to always pass — see
    TestSetupCommandCleanHome's setUp docstring for why (Fix A / -P closes
    the cwd fail-open these tests previously rode on for sys.executable).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.verify_patch = patch("cadre.provision.verify_importable", return_value=True)
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def _config_value(self):
        config_path = Path(self.tmp) / ".cadre" / "config"
        content = config_path.read_text(encoding="utf-8")
        line = next(ln for ln in content.splitlines() if ln.startswith("CADRE_HERMES_PYTHON="))
        return line.split("=", 1)[1]

    def test_records_sys_executable_by_default(self):
        code, out = setup_command(None, env={})
        self.assertEqual(code, 0, out)
        self.assertEqual(self._config_value(), sys.executable)

    def test_arg_beats_env(self):
        code, out = setup_command(sys.executable, env={"CADRE_HERMES_PYTHON": "/should/not/be/used"})
        self.assertEqual(code, 0, out)
        self.assertEqual(self._config_value(), sys.executable)

    def test_env_used_when_no_arg(self):
        code, out = setup_command(None, env={"CADRE_HERMES_PYTHON": sys.executable})
        self.assertEqual(code, 0, out)
        self.assertEqual(self._config_value(), sys.executable)


class TestSetupCommandExpanduser(unittest.TestCase):
    """A literal ~-relative --venv-python is expanduser'd before use (not an absolute tmp path)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_tilde_venv_python_is_expanduser_before_verify_and_record(self):
        """A stub placed under the (patched) $HOME, referenced via '~/bin/...', both
        verifies correctly (expanduser happens before the subprocess check) and is
        recorded as the expanded absolute path (not the literal '~/...' string)."""
        stub_dir = Path(self.tmp) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "fake-python"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o700)

        code, out = setup_command("~/bin/fake-python")
        self.assertEqual(code, 0, out)
        config_path = Path(self.tmp) / ".cadre" / "config"
        self.assertEqual(config_path.read_text(encoding="utf-8"), f"CADRE_HERMES_PYTHON={stub}\n")


class TestSetupCommandSymlinkGuard(unittest.TestCase):
    """cadre setup end-to-end: a pre-planted symlinked ~/.cadre/fleets is refused.

    Mocks the KTD11 gate (see TestSetupCommandCleanHome's setUp docstring) —
    this test is about the symlink-seeding guard, not the gate.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.verify_patch = patch("cadre.provision.verify_importable", return_value=True)
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def test_symlinked_fleets_dir_not_seeded_through(self):
        cadre_home = Path(self.tmp) / ".cadre"
        cadre_home.mkdir(mode=0o700, parents=True)
        elsewhere = Path(self.tmp) / "attacker-target"
        elsewhere.mkdir(mode=0o700)
        (cadre_home / "fleets").symlink_to(elsewhere, target_is_directory=True)

        code, out = setup_command(None)

        # Seeding degrades (warn-and-skip), never crashes — setup still succeeds
        # overall (config gets written; the other seeds proceed normally).
        self.assertEqual(code, 0, out)
        # Nothing was written through the attacker's symlink target.
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertTrue((cadre_home / "fleets").is_symlink())


class TestSetupCommandIdempotent(unittest.TestCase):
    """A second cadre setup run preserves operator edits; config stays grep|cut-parseable.

    Mocks the KTD11 gate (see TestSetupCommandCleanHome's setUp docstring) —
    idempotency across two setup_command(None) runs is what's under test here.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.verify_patch = patch("cadre.provision.verify_importable", return_value=True)
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def test_second_run_preserves_operator_edited_fleet(self):
        setup_command(None)
        cadre_home = Path(self.tmp) / ".cadre"
        edited = cadre_home / "fleets" / "research-swarm.yaml"
        edited.write_text("OPERATOR EDIT", encoding="utf-8")

        code, out = setup_command(None)
        self.assertEqual(code, 0, out)
        self.assertEqual(edited.read_text(encoding="utf-8"), "OPERATOR EDIT")

    def test_second_run_config_line_still_grep_cut_parses(self):
        setup_command(None)
        setup_command(None)  # second run
        config_path = Path(self.tmp) / ".cadre" / "config"
        content = config_path.read_text(encoding="utf-8")
        # Mirror the C2 shell contract: grep -E '^CADRE_HERMES_PYTHON=' | cut -d= -f2-
        line = next(ln for ln in content.splitlines() if ln.startswith("CADRE_HERMES_PYTHON="))
        value = line.split("=", 1)[1]
        self.assertEqual(value, sys.executable)
        self.assertEqual(content, f"CADRE_HERMES_PYTHON={sys.executable}\n")


class TestSetupCommandCLIWiring(unittest.TestCase):
    """`cadre setup` is reachable through cli.main()'s argparse dispatch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home_patch = patch.dict(os.environ, {"HOME": self.tmp})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_main_setup_subcommand_provisions_and_prints_result(self):
        # This test is about argparse dispatch (no --venv-python -> records
        # sys.executable), not the KTD11 gate — mocked for the same reason as
        # TestSetupCommandCleanHome (see its setUp docstring). The sibling
        # test below passes a real stub and deliberately does NOT mock this,
        # so it still exercises the real gate end-to-end.
        buf = io.StringIO()
        with patch("cadre.provision.verify_importable", return_value=True):
            with contextlib.redirect_stdout(buf):
                code = cli_main(["setup"])
        self.assertEqual(code, 0)
        self.assertTrue((Path(self.tmp) / ".cadre" / "config").exists())
        self.assertIn(str(Path(self.tmp) / ".cadre"), buf.getvalue())

    def test_main_setup_venv_python_flag_is_wired(self):
        stub = _make_stub_python(self, 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(["setup", "--venv-python", stub])
        self.assertEqual(code, 0)
        config_path = Path(self.tmp) / ".cadre" / "config"
        self.assertEqual(config_path.read_text(encoding="utf-8"), f"CADRE_HERMES_PYTHON={stub}\n")


# ---------------------------------------------------------------------------
# U5: `cadre verify-palette` — a thin dispatch onto cadre.verify_palette.main(),
# which owns its own live-progress printing and exit code (unlike validate/setup's
# (code, out) shape). The dispatch test patches main() so it never triggers a real
# host verification pass; verify_palette's own logic is covered by test_palette.py.
# (docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md)
# ---------------------------------------------------------------------------


class TestVerifyPaletteCommandCLIWiring(unittest.TestCase):
    """`cadre verify-palette` is reachable through cli.main()'s argparse dispatch
    and propagates cadre.verify_palette.main()'s exit code verbatim."""

    def test_main_verify_palette_subcommand_calls_verify_palette_main(self):
        with patch("cadre.verify_palette.main", return_value=0) as fake_main:
            code = cli_main(["verify-palette"])
        fake_main.assert_called_once_with()
        self.assertEqual(code, 0)

    def test_main_verify_palette_propagates_nonzero_exit(self):
        with patch("cadre.verify_palette.main", return_value=1):
            code = cli_main(["verify-palette"])
        self.assertEqual(code, 1)


# ---------------------------------------------------------------------------
# U6: `cadre install-skill` — a thin dispatch onto cadre.install_skill.
# install_skill()'s own behavior (guards, atomic swap, copy fallback) is
# covered by tests/test_install_skill.py; this only proves the argparse wiring
# and exit-code propagation (R11).
# ---------------------------------------------------------------------------


class TestInstallSkillCommandCLIWiring(unittest.TestCase):
    """`cadre install-skill` is reachable through cli.main()'s argparse dispatch
    and propagates install_skill()'s exit code verbatim."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.skills_dir = str(Path(self.tmp) / "skills")

    def test_main_install_skill_subcommand_creates_symlink(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(["install-skill", "--skills-dir", self.skills_dir])
        self.assertEqual(code, 0)
        entry = Path(self.skills_dir) / "cadre-fleet"
        self.assertTrue(entry.is_symlink())
        self.assertIn("installed cadre-fleet skill", buf.getvalue())

    def test_main_install_skill_propagates_nonzero_exit(self):
        with patch("cadre.cli.install_skill", return_value=(1, "boom")) as fake:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli_main(["install-skill", "--skills-dir", self.skills_dir])
        fake.assert_called_once_with(self.skills_dir)
        self.assertEqual(code, 1)
        self.assertIn("boom", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
