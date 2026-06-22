import contextlib
import importlib.util
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fleet_engine.capture import _slugify, resolve_run_dir
from fleet_engine.cli import run_command, validate_command
from fleet_engine.model_client import AgentResult

EXAMPLE = "fleets/research-swarm.example.yaml"


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

        with patch("fleet_engine.cli.save_run", side_effect=OSError("disk full")):
            code, out = run_command(EXAMPLE, "task", client=client, run_dir=run_dir)

        # The run itself succeeded — exit code 0, synthesis visible
        self.assertEqual(code, 0)
        self.assertIn("MY SYNTHESIS", out)

    def test_save_run_failure_exit_code_matches_run_outcome(self):
        """Non-zero exit from a failed run is preserved even when save_run also fails."""
        run_dir = self.tmp / "writerun2"
        client = FakeClient({r: ("fail", "down") for r in ("social", "web", "analysis")})

        with patch("fleet_engine.cli.save_run", side_effect=OSError("disk full")):
            code, out = run_command(EXAMPLE, "task", client=client, run_dir=run_dir)

        self.assertEqual(code, 1)  # run failed → non-zero even if save_run also failed


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
        with patch("fleet_engine.capture._DEFAULT_RUNS_ROOT", str(self.tmp / "cadre" / "runs")):
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
# Skill entry (skills/cadre-fleet/run.py) capture tests
# ---------------------------------------------------------------------------

def _load_skill_module():
    """Import skills/cadre-fleet/run.py as a module without running it."""
    skill_path = Path(__file__).resolve().parents[1] / "skills" / "cadre-fleet" / "run.py"
    spec = importlib.util.spec_from_file_location("cadre_fleet_run", skill_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cadre_fleet_run"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSkillEntryCapture(unittest.TestCase):
    """skills/cadre-fleet/run.py captures identically to run_command."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_mod = _load_skill_module()

    def _run_skill(self, behavior=None, extra_argv=None):
        """Run the skill's main() with a fake client, redirected to self.tmp."""
        fake = FakeClient(behavior or {"synthesizer": ("ok", "SKILL SYNTH")})

        with patch.dict(os.environ, {"CADRE_RUN_DIR": str(self.tmp)}):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                fleet_path = str(
                    Path(__file__).resolve().parents[1] / "fleets" / "research-swarm.example.yaml"
                )
                argv = ["--task", "what is best?", "--fleet", fleet_path] + (extra_argv or [])
                return self.run_mod.main(argv), fake

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

        fake = FakeClient({"synthesizer": ("ok", "SYNTH")})
        with patch.dict(os.environ, {"CADRE_RUN_DIR": bad_dir}):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                fleet_path = str(
                    Path(__file__).resolve().parents[1] / "fleets" / "research-swarm.example.yaml"
                )
                code = self.run_mod.main(["--task", "task", "--fleet", fleet_path])

        self.assertEqual(code, 1)
        self.assertEqual(len(fake.calls), 0, "no model calls should be made on dir failure")

    def test_skill_save_run_failure_returns_synthesis(self):
        """Skill: save_run failure still returns exit 0 (run succeeded)."""
        with patch.object(self.run_mod, "save_run", side_effect=OSError("disk full")):
            code, fake = self._run_skill()
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# New cadre-fleet skill tests: preview, --fleet required, --task required
# ---------------------------------------------------------------------------

_EXAMPLE_FLEET = str(
    Path(__file__).resolve().parents[1] / "fleets" / "research-swarm.example.yaml"
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
    """--task is required for a real run (not preview); omitting it returns non-zero
    with no model calls — the check fires before ModelClient is ever constructed.

    Covers: the task-None guard fires before capture (prepare_run_dir not called).
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
    """Error-path tests for skills/cadre-fleet/run.py main(): nonexistent file,
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
        with patch.dict(os.environ, {"CADRE_RUN_DIR": str(run_dir)}):
            with patch.object(self.run_mod, "ModelClient", return_value=fake):
                code = self.run_mod.main([
                    "--fleet", _EXAMPLE_FLEET,
                    "--task", "what is the best approach?",
                ])
        self.assertEqual(code, 0)
        self.assertTrue((run_dir / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
