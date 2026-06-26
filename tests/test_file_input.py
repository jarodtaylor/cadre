"""Tests for fleet_engine/file_input.py — the caller-layer file reader/composer (U1).

Test-first: the failure modes (missing / oversize / non-UTF-8 / ~-path) are the
load-bearing behavior. All tests use a real temporary directory so the read site
is exercised against the actual filesystem, not mocks.

The composer is a pure caller-layer helper: it reads the named --doc files and
composes their text into the task. It mirrors personas.resolve's error-accumulation
(collect per-file errors, raise ONE ConfigError) but returns a string rather than
mutating config. The engine never imports it (TestEngineIsolation, in
tests/test_personas.py, guards that).
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from fleet_engine.config import ConfigError
from fleet_engine.file_input import MAX_FILE_BYTES, compose


def _write(path: str, data, *, binary: bool = False) -> None:
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as f:
        f.write(data)


class TestComposeNoDocs(unittest.TestCase):
    """Empty doc list is a no-op: the base task passes through verbatim, zero I/O (R3, AE6)."""

    def test_plain_task_unchanged(self):
        task, paths = compose("just a literal task", [])
        self.assertEqual(task, "just a literal task")
        self.assertEqual(paths, [])

    def test_none_task_passes_through(self):
        task, paths = compose(None, [])
        self.assertIsNone(task)
        self.assertEqual(paths, [])

    def test_no_file_io_when_doc_list_empty(self):
        """With no docs, the helper must never call open() — proves the early return (R3)."""
        with patch("builtins.open", side_effect=AssertionError("compose opened a file with no docs")):
            task, paths = compose("task", [])
        self.assertEqual(task, "task")
        self.assertEqual(paths, [])


class TestComposeOneDoc(unittest.TestCase):
    """A single --doc file is read into a labeled block appended to the task (R1, AE1)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        self.doc = os.path.join(self.tmp, "plan.md")
        _write(self.doc, "# The Plan\n\nDo the thing.\n")

    def test_content_present_in_composed_task(self):
        task, _paths = compose("review this", [self.doc])
        self.assertIn("# The Plan", task)
        self.assertIn("Do the thing.", task)

    def test_block_labeled_with_path(self):
        task, _paths = compose("review this", [self.doc])
        self.assertIn(self.doc, task, "the block must be labeled with its source path (R2)")

    def test_base_task_preserved(self):
        task, _paths = compose("review this", [self.doc])
        self.assertIn("review this", task)

    def test_resolved_paths_returned(self):
        _task, paths = compose("review this", [self.doc])
        self.assertEqual(paths, [self.doc])

    def test_none_base_task_yields_block_only(self):
        """Base task None + one doc → composed task is the single labeled block (no None text)."""
        task, _paths = compose(None, [self.doc])
        self.assertIsNotNone(task)
        self.assertIn("# The Plan", task)
        self.assertNotIn("None", task)


class TestComposeMultipleDocs(unittest.TestCase):
    """Multiple --doc files are read in flag order, each in its own labeled block (R2, AE1)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        self.a = os.path.join(self.tmp, "a.md")
        self.b = os.path.join(self.tmp, "b.md")
        _write(self.a, "AAA content")
        _write(self.b, "BBB content")

    def test_both_blocks_present(self):
        task, _paths = compose("base", [self.a, self.b])
        self.assertIn("AAA content", task)
        self.assertIn("BBB content", task)

    def test_blocks_in_flag_order(self):
        task, _paths = compose("base", [self.a, self.b])
        self.assertLess(task.index("AAA content"), task.index("BBB content"))

    def test_reversed_flag_order_reverses_blocks(self):
        task, _paths = compose("base", [self.b, self.a])
        self.assertLess(task.index("BBB content"), task.index("AAA content"))

    def test_each_block_labeled(self):
        task, _paths = compose("base", [self.a, self.b])
        self.assertIn(self.a, task)
        self.assertIn(self.b, task)

    def test_resolved_paths_in_order(self):
        _task, paths = compose("base", [self.a, self.b])
        self.assertEqual(paths, [self.a, self.b])


class TestComposeMissingFile(unittest.TestCase):
    """A missing/unreadable --doc fails loud, naming the path; no silent empty injection (R5, AE2)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def test_missing_file_raises_config_error_naming_path(self):
        missing = os.path.join(self.tmp, "nope.md")
        with self.assertRaises(ConfigError) as ctx:
            compose("task", [missing])
        self.assertIn(missing, str(ctx.exception))

    def test_no_silent_empty_injection(self):
        """The helper must RAISE on a missing file, not return an empty block."""
        missing = os.path.join(self.tmp, "ghost.md")
        with self.assertRaises(ConfigError):
            compose("task", [missing])

    def test_two_missing_files_accumulate_into_one_error(self):
        """Per-file errors accumulate; a single ConfigError names BOTH paths (mirrors resolve)."""
        m1 = os.path.join(self.tmp, "one.md")
        m2 = os.path.join(self.tmp, "two.md")
        with self.assertRaises(ConfigError) as ctx:
            compose("task", [m1, m2])
        msg = str(ctx.exception)
        self.assertIn(m1, msg)
        self.assertIn(m2, msg)

    def test_directory_path_is_a_read_error(self):
        """A directory passed as --doc is unreadable → named error, never a traceback."""
        with self.assertRaises(ConfigError) as ctx:
            compose("task", [self.tmp])
        self.assertIn(self.tmp, str(ctx.exception))


class TestComposeOversize(unittest.TestCase):
    """A file over MAX_FILE_BYTES is truncated with a visible note — no unbounded inject (R6, AE3)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def test_oversize_file_truncated_with_note(self):
        big = os.path.join(self.tmp, "big.md")
        _write(big, "x" * (MAX_FILE_BYTES + 5000))
        task, _paths = compose(None, [big])
        # A truncation note is visibly present in the block.
        self.assertIn("truncat", task.lower())
        # The injected content is bounded — nowhere near the full oversize length.
        self.assertLess(len(task), MAX_FILE_BYTES + 2000)

    def test_under_limit_file_not_truncated(self):
        small = os.path.join(self.tmp, "small.md")
        _write(small, "y" * 100)
        task, _paths = compose(None, [small])
        self.assertNotIn("truncat", task.lower())
        self.assertIn("y" * 100, task)


class TestComposeNonUtf8(unittest.TestCase):
    """A non-UTF-8 / binary file is a loud error naming the file; raw bytes never returned (R6, AE5)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def test_binary_file_raises_naming_file(self):
        binf = os.path.join(self.tmp, "image.bin")
        _write(binf, b"\xff\xfe\x00\x01\x80\x81 not text", binary=True)
        with self.assertRaises(ConfigError) as ctx:
            compose("task", [binf])
        self.assertIn(binf, str(ctx.exception))

    def test_invalid_utf8_does_not_leak_bytes(self):
        """The decode error is caught at the read site — no raw-bytes string escapes."""
        binf = os.path.join(self.tmp, "bad.dat")
        _write(binf, b"\xc3\x28\xa0\xa1", binary=True)  # invalid UTF-8 sequences
        with self.assertRaises(ConfigError):
            compose(None, [binf])

    def test_oversize_binary_still_errors(self):
        """An oversize non-UTF-8 file must still error — truncation must not mask AE5."""
        binf = os.path.join(self.tmp, "bigbad.dat")
        _write(binf, b"\xff\xfe" * (MAX_FILE_BYTES), binary=True)
        with self.assertRaises(ConfigError):
            compose(None, [binf])


class TestComposeTildePath(unittest.TestCase):
    """A ~-relative --doc path resolves and reads — guards the expanduser footgun (KTD4).

    An absolute-only suite is blind to this: open('~/x') does NOT expand ~.
    """

    def test_tilde_path_resolves_and_reads(self):
        with tempfile.TemporaryDirectory() as home:
            home = os.path.realpath(home)
            doc = os.path.join(home, "notes.md")
            _write(doc, "tilde body text")
            with patch.dict(os.environ, {"HOME": home}):
                task, paths = compose(None, ["~/notes.md"])
            self.assertIn("tilde body text", task)
            # The label/returned path stays as the caller named it (~/notes.md),
            # not the expanded absolute path.
            self.assertIn("~/notes.md", paths)


class TestComposeUtf8Content(unittest.TestCase):
    """Legitimate UTF-8 content (accents, CJK, em-dash) round-trips untouched.

    Injected content is NOT sanitized (KTD6) — corrupting the reviewer's document
    would defeat the feature; output-side hardening is the deferred #5/#23 surface.
    """

    def test_unicode_content_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, "u.md")
            _write(doc, "résumé — 日本語 — naïve")
            task, _paths = compose(None, [doc])
            self.assertIn("résumé — 日本語 — naïve", task)


if __name__ == "__main__":
    unittest.main()
