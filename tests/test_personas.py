"""Tests for cadre/personas.py — caller-layer persona resolver (U2).

All tests use a real temporary directory so confinement invariants are
exercised against the actual filesystem, not mocks.  macOS `tempfile`
uses /var/folders/... which is a symlink to /private/var — we realpath
the tmp root at fixture setup so path comparisons are stable on darwin.
"""

import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from cadre.config import ConfigError, FleetConfig, SpecialistSpec
from cadre.personas import DEFAULT_PERSONAS_DIR, default_pool_dir, resolve


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_collect_config(specialists: list[dict]) -> FleetConfig:
    """Return a collect FleetConfig for the given specialist dicts."""
    return FleetConfig.from_dict({
        "name": "test-fleet",
        "convergence": "collect",
        "specialists": specialists,
    })


def _focus_spec(role: str = "analyst", focus: str = "write a report") -> dict:
    return {"role": role, "provider": "openai", "model": "gpt-4o", "focus": focus}


def _persona_spec(name: str, role: str = "reviewer") -> dict:
    return {"role": role, "provider": "openai", "model": "gpt-4o", "persona": name}


class TestFocusOnlyPassthrough(unittest.TestCase):
    """Focus-only fleets: effective_instruction == focus, NO file I/O."""

    def test_focus_set_as_effective_instruction(self):
        cfg = _make_collect_config([_focus_spec(focus="analyze the inputs carefully")])
        # Point pool_dir at a path that does NOT exist — must still resolve cleanly (R4).
        result = resolve(cfg, "/nonexistent/pool/dir")
        self.assertEqual(cfg.specialists[0].effective_instruction, "analyze the inputs carefully")
        self.assertIs(result, cfg)  # returns the same object

    def test_multiple_focus_specs_all_set(self):
        cfg = _make_collect_config([
            _focus_spec(role="a", focus="task A"),
            _focus_spec(role="b", focus="task B"),
        ])
        resolve(cfg, "/nonexistent/pool/dir")
        self.assertEqual(cfg.specialists[0].effective_instruction, "task A")
        self.assertEqual(cfg.specialists[1].effective_instruction, "task B")

    def test_absent_pool_dir_focus_only_no_error(self):
        """Absent pool_dir with all focus-only specs resolves fine — zero I/O."""
        cfg = _make_collect_config([_focus_spec()])
        # Should NOT raise ConfigError.
        resolve(cfg, "/completely/made/up/path")
        self.assertEqual(cfg.specialists[0].effective_instruction, "write a report")


class TestPersonaResolution(unittest.TestCase):
    """Happy-path: a named persona file is read into effective_instruction."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Realpath the pool so darwin /var→/private/var symlink doesn't bite comparisons.
        self.pool = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_persona(self, name: str, content: str) -> None:
        path = os.path.join(self.pool, name + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_persona_file_text_becomes_effective_instruction(self):
        """R3: a specialist naming a seeded persona gets that file's exact text."""
        body = "# Coherence Reviewer\n\nCheck the doc for internal consistency.\n"
        self._write_persona("coherence-reviewer", body)
        cfg = _make_collect_config([_persona_spec("coherence-reviewer")])
        resolve(cfg, self.pool)
        self.assertEqual(cfg.specialists[0].effective_instruction, body)

    def test_persona_text_is_raw_not_sanitized(self):
        """Resolver stores raw markdown; render layer sanitizes (KTD6)."""
        body = "# Title\n\n**Bold** and _italic_ text.\n\nMultiple paragraphs.\n"
        self._write_persona("rich-persona", body)
        cfg = _make_collect_config([_persona_spec("rich-persona")])
        resolve(cfg, self.pool)
        self.assertEqual(cfg.specialists[0].effective_instruction, body)

    def test_mixed_persona_and_focus_specs(self):
        """Persona and focus specs can coexist in the same fleet."""
        body = "You are an adversarial reviewer. Probe for gaps.\n"
        self._write_persona("adversarial", body)
        cfg = _make_collect_config([
            _persona_spec("adversarial", role="adversary"),
            _focus_spec(role="scribe", focus="summarize the findings"),
        ])
        resolve(cfg, self.pool)
        self.assertEqual(cfg.specialists[0].effective_instruction, body)
        self.assertEqual(cfg.specialists[1].effective_instruction, "summarize the findings")

    def test_returns_config_object(self):
        """resolve() returns the same config object (convenience for one-liner callers)."""
        body = "A valid persona.\n"
        self._write_persona("simple", body)
        cfg = _make_collect_config([_persona_spec("simple")])
        result = resolve(cfg, self.pool)
        self.assertIs(result, cfg)


class TestMissingAndEmptyPersona(unittest.TestCase):
    """R8, R5: missing / empty / whitespace-only files produce clear ConfigError."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pool = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_persona_file_raises_config_error(self):
        """R8: a missing persona file raises ConfigError with the name in the message."""
        cfg = _make_collect_config([_persona_spec("missing-persona")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        self.assertTrue(any("missing-persona" in e for e in ctx.exception.errors))

    def test_empty_persona_file_raises_config_error(self):
        """R5: an empty file is a loud error, not a silent empty lane."""
        path = os.path.join(self.pool, "empty.md")
        with open(path, "w") as f:
            f.write("")
        cfg = _make_collect_config([_persona_spec("empty")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        self.assertTrue(any("empty" in e for e in ctx.exception.errors))

    def test_whitespace_only_persona_file_raises_config_error(self):
        """R5: a whitespace-only file is also a loud error."""
        path = os.path.join(self.pool, "whitespace.md")
        with open(path, "w") as f:
            f.write("   \n\t\n  ")
        cfg = _make_collect_config([_persona_spec("whitespace")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        self.assertTrue(any("whitespace" in e for e in ctx.exception.errors))

    def test_absent_pool_dir_with_persona_raises_config_error(self):
        """Absent pool_dir + a persona spec → ConfigError naming the bad path."""
        cfg = _make_collect_config([_persona_spec("some-persona")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, "/nonexistent/pool/path")
        self.assertTrue(any("not found" in e or "directory" in e for e in ctx.exception.errors))

    def test_errors_accumulate_across_multiple_specialists(self):
        """Two failing persona specs → both errors in one ConfigError."""
        cfg = _make_collect_config([
            _persona_spec("missing-one", role="r1"),
            _persona_spec("missing-two", role="r2"),
        ])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        errors = ctx.exception.errors
        self.assertTrue(any("missing-one" in e for e in errors))
        self.assertTrue(any("missing-two" in e for e in errors))

    def test_raises_config_error_type_not_raw_exception(self):
        """Every failure mode raises ConfigError, never a raw OSError or IOError."""
        cfg = _make_collect_config([_persona_spec("no-such-file")])
        with self.assertRaises(ConfigError):
            resolve(cfg, self.pool)


class TestConfinementAndSymlinks(unittest.TestCase):
    """R10: out-of-pool reads are refused (realpath-confine + O_NOFOLLOW)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pool = os.path.realpath(self._tmp.name)
        # A file OUTSIDE the pool that attacker controls.
        self._outside_tmp = tempfile.TemporaryDirectory()
        self.outside_root = os.path.realpath(self._outside_tmp.name)
        self.outside_file = os.path.join(self.outside_root, "secret.txt")
        with open(self.outside_file, "w") as f:
            f.write("SECRET CONTENT THAT MUST NOT BE READ\n")

    def tearDown(self):
        self._tmp.cleanup()
        self._outside_tmp.cleanup()

    def test_symlink_in_pool_pointing_outside_is_refused(self):
        """R10: a symlink at <pool>/evil.md → outside file is refused; content not read."""
        link_path = os.path.join(self.pool, "evil.md")
        os.symlink(self.outside_file, link_path)
        cfg = _make_collect_config([_persona_spec("evil")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        errors = ctx.exception.errors
        self.assertTrue(any("evil" in e for e in errors))
        # Confirm the outside file's content never appeared in effective_instruction.
        for spec in cfg.specialists:
            self.assertNotIn("SECRET CONTENT", spec.effective_instruction)

    def test_symlink_refused_exercises_realpath_confine_and_o_nofollow(self):
        """The realpath-confine path is exercised (name whose realpath escapes pool)."""
        # A symlink that points outside: realpath(candidate) lands outside pool_real.
        link_path = os.path.join(self.pool, "traversal.md")
        os.symlink(self.outside_file, link_path)
        cfg = _make_collect_config([_persona_spec("traversal")])
        with self.assertRaises(ConfigError):
            resolve(cfg, self.pool)

    def test_valid_persona_while_symlink_in_pool_still_resolves_valid(self):
        """A valid persona alongside a bad symlink: valid resolves, bad errors."""
        # Write a valid persona.
        good_content = "Valid persona content.\n"
        with open(os.path.join(self.pool, "good-persona.md"), "w") as f:
            f.write(good_content)
        # Plant an escaping symlink.
        os.symlink(self.outside_file, os.path.join(self.pool, "evil.md"))
        cfg = _make_collect_config([
            _persona_spec("good-persona", role="r1"),
            _persona_spec("evil", role="r2"),
        ])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        errors = ctx.exception.errors
        self.assertTrue(any("evil" in e for e in errors))
        # good-persona should have resolved before the raise.
        self.assertEqual(cfg.specialists[0].effective_instruction, good_content)

    def test_in_pool_symlink_is_refused_by_o_nofollow(self):
        """O_NOFOLLOW belt: a symlink whose target is INSIDE the pool passes the
        realpath-confine check (commonpath sees an in-pool target) yet is still
        refused at open (ELOOP). This guards the O_NOFOLLOW flag against silent
        removal — without it the link would be followed and read, and the file's
        text would land in effective_instruction instead of raising. The realpath
        confine alone does NOT cover this case; only O_NOFOLLOW does."""
        target = os.path.join(self.pool, "real-target.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("IN-POOL TARGET CONTENT via symlink.\n")
        link_path = os.path.join(self.pool, "inside-link.md")
        os.symlink(target, link_path)  # target is inside the pool → commonpath passes
        cfg = _make_collect_config([_persona_spec("inside-link")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        self.assertTrue(any("inside-link" in e for e in ctx.exception.errors))
        # The symlink must NOT have been followed and read.
        self.assertNotIn("IN-POOL TARGET CONTENT", cfg.specialists[0].effective_instruction)


class TestNonUtf8AndUnreadable(unittest.TestCase):
    """Non-UTF-8 and unreadable personas: clear error, never traceback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pool = os.path.realpath(self._tmp.name)

    def tearDown(self):
        # Restore permissions before cleanup so TemporaryDirectory.cleanup() can delete.
        for fname in os.listdir(self.pool):
            fpath = os.path.join(self.pool, fname)
            try:
                os.chmod(fpath, 0o644)
            except OSError:
                pass
        self._tmp.cleanup()

    def test_non_utf8_persona_raises_config_error(self):
        """A non-UTF-8 binary file produces ConfigError, not a UnicodeDecodeError traceback."""
        path = os.path.join(self.pool, "binary.md")
        # Write raw bytes that are invalid UTF-8.
        with open(path, "wb") as f:
            f.write(b"\xff\xfe binary garbage \x80\x81\x82")
        cfg = _make_collect_config([_persona_spec("binary")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        self.assertTrue(any("binary" in e for e in ctx.exception.errors))

    @unittest.skipIf(getattr(os, "getuid", lambda: 0)() == 0, "root can read 0o000 files — skip")
    def test_unreadable_persona_raises_config_error(self):
        """A chmod 0o000 file produces ConfigError, not an OSError traceback."""
        path = os.path.join(self.pool, "locked.md")
        with open(path, "w") as f:
            f.write("some content\n")
        os.chmod(path, 0o000)
        cfg = _make_collect_config([_persona_spec("locked")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        self.assertTrue(any("locked" in e for e in ctx.exception.errors))

    def test_directory_in_pool_raises_config_error(self):
        """A persona name resolving to a directory (not a file) raises ConfigError."""
        # Create a directory named like a persona file.
        dir_path = os.path.join(self.pool, "not-a-file.md")
        os.makedirs(dir_path)
        cfg = _make_collect_config([_persona_spec("not-a-file")])
        with self.assertRaises(ConfigError) as ctx:
            resolve(cfg, self.pool)
        self.assertTrue(any("not-a-file" in e for e in ctx.exception.errors))


class TestDefaultPoolDir(unittest.TestCase):
    """default_pool_dir() returns CADRE_PERSONAS_DIR env var first, then DEFAULT_PERSONAS_DIR."""

    def test_no_env_returns_default(self):
        """Without CADRE_PERSONAS_DIR set, default_pool_dir returns DEFAULT_PERSONAS_DIR."""
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_PERSONAS_DIR"}
        with patch.dict(os.environ, env_without, clear=True):
            result = default_pool_dir()
        self.assertEqual(result, DEFAULT_PERSONAS_DIR)

    def test_env_override_returned(self):
        """CADRE_PERSONAS_DIR env var is used when set, regardless of DEFAULT_PERSONAS_DIR."""
        with patch.dict(os.environ, {"CADRE_PERSONAS_DIR": "/custom/personas"}):
            result = default_pool_dir()
        self.assertEqual(result, "/custom/personas")

    def test_empty_env_falls_through_to_default(self):
        """An empty CADRE_PERSONAS_DIR string behaves the same as unset — falls through."""
        with patch.dict(os.environ, {"CADRE_PERSONAS_DIR": ""}):
            result = default_pool_dir()
        self.assertEqual(result, DEFAULT_PERSONAS_DIR)


def _static_imports(mod) -> list[str]:
    """Return every module name statically imported by ``mod`` (AST, not runtime).

    AST analysis of the source file is immune to test-ordering effects from other
    test modules that do import cadre.engine.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mod))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)
    return imported


class TestEngineIsolation(unittest.TestCase):
    """KTD1: caller-layer modules and the engine stay on opposite sides of the seam.

    The engine core (engine.py, model_client.py) must not import the caller-layer
    helpers (personas, file_input), and those helpers must not import the engine —
    so the engine receives only finished strings and gains no file I/O or
    fleet-domain knowledge (R8;
    docs/solutions/architecture-patterns/side-effects-at-the-edge-pure-engine-core.md).
    """

    def test_personas_does_not_import_engine_or_model_client(self):
        """cadre/personas.py must NOT import engine or model_client (KTD1)."""
        import cadre.personas as p_mod

        imported = _static_imports(p_mod)
        self.assertNotIn(
            "cadre.engine",
            imported,
            "personas.py must not import engine (KTD1 — engine-purity constraint)",
        )
        self.assertNotIn(
            "cadre.model_client",
            imported,
            "personas.py must not import model_client (KTD1 — engine-purity constraint)",
        )

    def test_file_input_does_not_import_engine_or_model_client(self):
        """cadre/file_input.py must NOT import engine or model_client (KTD2)."""
        import cadre.file_input as fi_mod

        imported = _static_imports(fi_mod)
        self.assertNotIn(
            "cadre.engine",
            imported,
            "file_input.py must not import engine (KTD2 — engine stays path-free)",
        )
        self.assertNotIn(
            "cadre.model_client",
            imported,
            "file_input.py must not import model_client (KTD2 — engine stays path-free)",
        )

    def test_engine_does_not_import_file_input(self):
        """cadre/engine.py must NOT import file_input (KTD2 / R8).

        The engine must never gain file-reading logic — it sees only the composed
        task string. A stray import here is the architecture regression this guards.
        """
        import cadre.engine as e_mod

        imported = _static_imports(e_mod)
        self.assertNotIn(
            "cadre.file_input",
            imported,
            "engine.py must not import file_input (R8 — engine gains no file I/O)",
        )

    def test_judge_grade_does_not_import_engine_or_model_client(self):
        """cadre/judge_grade.py must NOT import engine or model_client.

        The judge-grade parser is caller-layer: the engine returns the judge's
        raw text and the edge parses it (KTD2). A stray engine import here would
        fold parsing back into the core.
        """
        import cadre.judge_grade as jg_mod

        imported = _static_imports(jg_mod)
        self.assertNotIn(
            "cadre.engine",
            imported,
            "judge_grade.py must not import engine (KTD2 — parsing lives at the edge)",
        )
        self.assertNotIn(
            "cadre.model_client",
            imported,
            "judge_grade.py must not import model_client (caller-layer parser)",
        )

    def test_engine_does_not_import_judge_grade(self):
        """cadre/engine.py must NOT import judge_grade (KTD2).

        The engine emits the judge's raw text and does no parsing; a stray import
        of the edge parser is the architecture regression this guards.
        """
        import cadre.engine as e_mod

        imported = _static_imports(e_mod)
        self.assertNotIn(
            "cadre.judge_grade",
            imported,
            "engine.py must not import judge_grade (KTD2 — engine returns raw text, edge parses)",
        )

    def test_model_client_does_not_import_file_input(self):
        """cadre/model_client.py must NOT import file_input (KTD2 / R8)."""
        import cadre.model_client as mc_mod

        imported = _static_imports(mc_mod)
        self.assertNotIn(
            "cadre.file_input",
            imported,
            "model_client.py must not import file_input (R8 — engine gains no file I/O)",
        )

    def test_approval_does_not_import_engine_or_model_client(self):
        """cadre/approval.py must NOT import engine or model_client (KTD5).

        approval.py type-hints its FleetConfig parameter under `TYPE_CHECKING`
        only, so there is no runtime import of engine/model_client here — this
        locks that by-construction guarantee against a careless future edit,
        mirroring the bidirectional coverage the file_input/judge_grade pairs get.
        """
        import cadre.approval as a_mod

        imported = _static_imports(a_mod)
        self.assertNotIn(
            "cadre.engine",
            imported,
            "approval.py must not import engine (KTD5 — engine-purity constraint)",
        )
        self.assertNotIn(
            "cadre.model_client",
            imported,
            "approval.py must not import model_client (KTD5 — engine-purity constraint)",
        )

    def test_engine_does_not_import_approval(self):
        """cadre/engine.py and model_client.py must NOT import approval (KTD5).

        Preview-bound approval / trust-boundary logic is caller-layer; a stray
        import here would fold trust-surface concerns back into the pure engine core.
        """
        import cadre.engine as e_mod
        import cadre.model_client as mc_mod

        imported_engine = _static_imports(e_mod)
        self.assertNotIn(
            "cadre.approval",
            imported_engine,
            "engine.py must not import approval (KTD5 — engine-purity constraint)",
        )
        imported_mc = _static_imports(mc_mod)
        self.assertNotIn(
            "cadre.approval",
            imported_mc,
            "model_client.py must not import approval (KTD5 — engine-purity constraint)",
        )

    def test_install_skill_does_not_import_engine_or_model_client(self):
        """cadre/install_skill.py must NOT import engine or model_client (U6).

        install_skill materializes the Hermes skill from package data — pure
        filesystem/trust-boundary logic (reuses approval._parent_is_safe),
        same posture as approval.py itself.
        """
        import cadre.install_skill as is_mod

        imported = _static_imports(is_mod)
        self.assertNotIn(
            "cadre.engine",
            imported,
            "install_skill.py must not import engine (engine-purity constraint)",
        )
        self.assertNotIn(
            "cadre.model_client",
            imported,
            "install_skill.py must not import model_client (engine-purity constraint)",
        )

    def test_engine_does_not_import_install_skill(self):
        """cadre/engine.py and model_client.py must NOT import install_skill (U6)."""
        import cadre.engine as e_mod
        import cadre.model_client as mc_mod

        imported_engine = _static_imports(e_mod)
        self.assertNotIn(
            "cadre.install_skill",
            imported_engine,
            "engine.py must not import install_skill (engine-purity constraint)",
        )
        imported_mc = _static_imports(mc_mod)
        self.assertNotIn(
            "cadre.install_skill",
            imported_mc,
            "model_client.py must not import install_skill (engine-purity constraint)",
        )

    def test_provision_does_not_import_engine_or_model_client(self):
        """cadre/provision.py must NOT import engine or model_client (U4).

        provision.py scaffolds ~/.cadre, seeds starter data, writes config, and
        subprocess-verifies importability — pure filesystem/trust-boundary
        logic, same posture as install_skill.py/approval.py.
        """
        import cadre.provision as prov_mod

        imported = _static_imports(prov_mod)
        self.assertNotIn(
            "cadre.engine",
            imported,
            "provision.py must not import engine (engine-purity constraint)",
        )
        self.assertNotIn(
            "cadre.model_client",
            imported,
            "provision.py must not import model_client (engine-purity constraint)",
        )

    def test_engine_does_not_import_provision(self):
        """cadre/engine.py and model_client.py must NOT import provision (U4)."""
        import cadre.engine as e_mod
        import cadre.model_client as mc_mod

        imported_engine = _static_imports(e_mod)
        self.assertNotIn(
            "cadre.provision",
            imported_engine,
            "engine.py must not import provision (engine-purity constraint)",
        )
        imported_mc = _static_imports(mc_mod)
        self.assertNotIn(
            "cadre.provision",
            imported_mc,
            "model_client.py must not import provision (engine-purity constraint)",
        )

    def test_verify_palette_does_not_import_engine_or_model_client(self):
        """cadre/verify_palette.py must NOT import engine or model_client (U5).

        verify_palette makes live host calls to confirm (provider, model)
        candidates and writes ~/.cadre/palette.yaml — caller-layer, same
        posture as provision.py/install_skill.py.
        """
        import cadre.verify_palette as vp_mod

        imported = _static_imports(vp_mod)
        self.assertNotIn(
            "cadre.engine",
            imported,
            "verify_palette.py must not import engine (engine-purity constraint)",
        )
        self.assertNotIn(
            "cadre.model_client",
            imported,
            "verify_palette.py must not import model_client (engine-purity constraint)",
        )

    def test_engine_does_not_import_verify_palette(self):
        """cadre/engine.py and model_client.py must NOT import verify_palette (U5)."""
        import cadre.engine as e_mod
        import cadre.model_client as mc_mod

        imported_engine = _static_imports(e_mod)
        self.assertNotIn(
            "cadre.verify_palette",
            imported_engine,
            "engine.py must not import verify_palette (engine-purity constraint)",
        )
        imported_mc = _static_imports(mc_mod)
        self.assertNotIn(
            "cadre.verify_palette",
            imported_mc,
            "model_client.py must not import verify_palette (engine-purity constraint)",
        )


class TestPoolDirTildeExpansion(unittest.TestCase):
    """resolve() expanduser's a ~-prefixed pool_dir before realpath (regression).

    The default pool is the literal "~/.cadre/personas" (from default_pool_dir);
    os.path.realpath does NOT expand ~, so without an expanduser step the default
    would canonicalize to <cwd>/~/.cadre/personas and never match the
    install-seeded pool — every CLI/skill run of a persona fleet would raise.
    The other tests pass already-absolute pool_dirs, which mask this; these use a
    ~-relative pool with HOME patched at a tmp dir.
    """

    def test_tilde_pool_dir_is_expanded(self):
        """A literal "~/..." pool_dir resolves against the expanded home."""
        with tempfile.TemporaryDirectory() as home:
            home = os.path.realpath(home)
            pool = os.path.join(home, ".cadre", "personas")
            os.makedirs(pool)
            with open(os.path.join(pool, "p.md"), "w", encoding="utf-8") as f:
                f.write("persona body text\n")
            cfg = _make_collect_config([_persona_spec("p")])
            with patch.dict(os.environ, {"HOME": home}):
                resolve(cfg, "~/.cadre/personas")  # ~ must expand to <home>/.cadre/personas
            self.assertEqual(
                cfg.specialists[0].effective_instruction, "persona body text\n"
            )

    def test_tilde_in_env_pool_dir_is_expanded(self):
        """A ~-containing CADRE_PERSONAS_DIR (via default_pool_dir) also expands."""
        with tempfile.TemporaryDirectory() as home:
            home = os.path.realpath(home)
            pool = os.path.join(home, "custom", "personas")
            os.makedirs(pool)
            with open(os.path.join(pool, "p.md"), "w", encoding="utf-8") as f:
                f.write("body\n")
            cfg = _make_collect_config([_persona_spec("p")])
            with patch.dict(os.environ, {"HOME": home, "CADRE_PERSONAS_DIR": "~/custom/personas"}):
                resolve(cfg, default_pool_dir())
            self.assertEqual(cfg.specialists[0].effective_instruction, "body\n")


class TestPoolPermissions(unittest.TestCase):
    """The pool dir is trusted as a model-instruction source, so it must be private
    (owned by the current user, not group/other-writable). Surfaced by Codex review."""

    def test_group_or_other_writable_pool_rejected(self):
        """A pool writable by group/other is refused — a local attacker could plant a file."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.realpath(tmp)
            with open(os.path.join(pool, "p.md"), "w", encoding="utf-8") as f:
                f.write("persona body\n")
            os.chmod(pool, 0o777)  # group/other writable → unsafe
            cfg = _make_collect_config([_persona_spec("p")])
            with self.assertRaises(ConfigError) as ctx:
                resolve(cfg, pool)
            self.assertTrue(
                any("unsafe" in e.lower() for e in ctx.exception.errors),
                f"expected an unsafe-pool error; got: {ctx.exception.errors}",
            )

    def test_owner_only_pool_accepted(self):
        """A 0o700 owner-only pool resolves normally — the safe default must not false-positive."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.realpath(tmp)
            os.chmod(pool, 0o700)
            with open(os.path.join(pool, "p.md"), "w", encoding="utf-8") as f:
                f.write("persona body\n")
            cfg = _make_collect_config([_persona_spec("p")])
            resolve(cfg, pool)  # must not raise
            self.assertEqual(cfg.specialists[0].effective_instruction, "persona body\n")


if __name__ == "__main__":
    unittest.main()
