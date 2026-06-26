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
        task, paths, _trunc = compose("just a literal task", [])
        self.assertEqual(task, "just a literal task")
        self.assertEqual(paths, [])

    def test_none_task_passes_through(self):
        task, paths, _trunc = compose(None, [])
        self.assertIsNone(task)
        self.assertEqual(paths, [])

    def test_no_file_io_when_doc_list_empty(self):
        """With no docs, the helper must never call open() — proves the early return (R3)."""
        with patch("builtins.open", side_effect=AssertionError("compose opened a file with no docs")):
            task, paths, _trunc = compose("task", [])
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
        task, _paths, _trunc = compose("review this", [self.doc])
        self.assertIn("# The Plan", task)
        self.assertIn("Do the thing.", task)

    def test_block_labeled_with_path(self):
        task, _paths, _trunc = compose("review this", [self.doc])
        self.assertIn(self.doc, task, "the block must be labeled with its source path (R2)")

    def test_base_task_preserved(self):
        task, _paths, _trunc = compose("review this", [self.doc])
        self.assertIn("review this", task)

    def test_resolved_paths_returned(self):
        _task, paths, _trunc = compose("review this", [self.doc])
        self.assertEqual(paths, [self.doc])

    def test_none_base_task_yields_block_only(self):
        """Base task None + one doc → composed task is the single labeled block (no None text)."""
        task, _paths, _trunc = compose(None, [self.doc])
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
        task, _paths, _trunc = compose("base", [self.a, self.b])
        self.assertIn("AAA content", task)
        self.assertIn("BBB content", task)

    def test_blocks_in_flag_order(self):
        task, _paths, _trunc = compose("base", [self.a, self.b])
        self.assertLess(task.index("AAA content"), task.index("BBB content"))

    def test_reversed_flag_order_reverses_blocks(self):
        task, _paths, _trunc = compose("base", [self.b, self.a])
        self.assertLess(task.index("BBB content"), task.index("AAA content"))

    def test_each_block_labeled(self):
        task, _paths, _trunc = compose("base", [self.a, self.b])
        self.assertIn(self.a, task)
        self.assertIn(self.b, task)

    def test_resolved_paths_in_order(self):
        _task, paths, _trunc = compose("base", [self.a, self.b])
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


class TestComposeSpecialFiles(unittest.TestCase):
    """Non-regular files error cleanly and NEVER hang (R6 'no lane hangs'); an empty
    regular file is read (a conscious content-not-instruction choice)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo not available on this platform")
    def test_fifo_errors_and_does_not_hang(self):
        """A FIFO --doc must error ('not a regular file'), not block at open() (R6).

        If the S_ISREG guard regressed, this would hang at open() waiting for a
        writer — the test would time out loudly rather than silently pass.
        """
        fifo = os.path.join(self.tmp, "pipe")
        os.mkfifo(fifo)
        with self.assertRaises(ConfigError) as ctx:
            compose("task", [fifo])
        msg = str(ctx.exception)
        self.assertIn(fifo, msg)
        self.assertIn("regular file", msg)

    def test_symlink_to_regular_file_is_followed_and_read(self):
        """KTD4 intentionally follows symlinks (no O_NOFOLLOW): a symlink to a regular
        file is read. Guards the decision — a later O_NOFOLLOW hardening would break it
        silently (same discipline as the ~-path test)."""
        real = os.path.join(self.tmp, "real.md")
        _write(real, "REAL_DOC_BODY")
        link = os.path.join(self.tmp, "link.md")
        os.symlink(real, link)
        task, paths, _trunc = compose(None, [link])
        self.assertIn("REAL_DOC_BODY", task)
        self.assertEqual(paths, [link])  # labeled with the as-named symlink path

    def test_empty_file_composes_empty_block_not_error(self):
        """An empty (0-byte) --doc is readable and composes an (empty) labeled block.

        A CONSCIOUS choice (advisor-confirmed): unlike personas.resolve, which errors
        on an empty INSTRUCTION, a --doc is CONTENT — the helper does not police whether
        the named document is empty. The labeled block is visible, not a silent drop.
        """
        empty = os.path.join(self.tmp, "empty.md")
        _write(empty, "")
        task, paths, _trunc = compose("review", [empty])
        self.assertIn(empty, task)        # the labeled block is present
        self.assertEqual(paths, [empty])  # readable → no error raised


class TestComposeOversize(unittest.TestCase):
    """A file over MAX_FILE_BYTES is truncated with a visible note — no unbounded inject (R6, AE3)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def test_oversize_file_truncated_with_note(self):
        big = os.path.join(self.tmp, "big.md")
        _write(big, "x" * (MAX_FILE_BYTES + 5000))
        task, _paths, _trunc = compose(None, [big])
        # A truncation note is visibly present in the block.
        self.assertIn("truncat", task.lower())
        # The injected content is bounded — nowhere near the full oversize length.
        self.assertLess(len(task), MAX_FILE_BYTES + 2000)

    def test_under_limit_file_not_truncated(self):
        small = os.path.join(self.tmp, "small.md")
        _write(small, "y" * 100)
        task, _paths, _trunc = compose(None, [small])
        self.assertNotIn("truncat", task.lower())
        self.assertIn("y" * 100, task)

    def test_truncation_note_pins_exact_text_and_kib(self):
        """The note states the exact 256 KiB cap — a wrong constant/format must fail."""
        big = os.path.join(self.tmp, "big.md")
        _write(big, "x" * (MAX_FILE_BYTES + 5000))
        task, _paths, _trunc = compose(None, [big])
        self.assertIn("[cadre: this file exceeded 256 KiB and was truncated", task)

    def test_exactly_max_bytes_not_truncated(self):
        """A file of EXACTLY MAX_FILE_BYTES is at the cap, not over it — no truncation
        (guards the > vs >= fence-post)."""
        exact = os.path.join(self.tmp, "exact.md")
        _write(exact, "z" * MAX_FILE_BYTES)
        task, _paths, _trunc = compose(None, [exact])
        self.assertNotIn("truncat", task.lower())
        self.assertIn("z" * 1000, task)  # full content present

    def test_multibyte_char_straddling_cap_decodes_cleanly(self):
        """When the byte cap splits a multibyte UTF-8 char, the partial tail is dropped
        (incremental decoder, final=False) rather than raising — the subtlest KTD5 path.

        If final were True here, the lone leading byte of the split char would raise
        UnicodeDecodeError and turn a valid oversize document into an error.
        """
        straddle = os.path.join(self.tmp, "straddle.md")
        # ASCII filler of MAX-1 bytes, then a 3-byte char ('日' = E6 97 A5). Truncating
        # to MAX bytes keeps the filler + only the first byte of the char.
        with open(straddle, "wb") as f:
            f.write(b"a" * (MAX_FILE_BYTES - 1))
            f.write("日".encode("utf-8"))
        # Must not raise; returns the clean ASCII prefix + truncation note.
        task, _paths, _trunc = compose(None, [straddle])
        self.assertIn("truncat", task.lower())
        self.assertIn("a" * 1000, task)
        self.assertNotIn("日", task)  # the split char was dropped, not mangled

    def test_oversize_path_reported_in_truncated_return(self):
        """compose's third return value lists ONLY the truncated paths, so the caller
        can disclose the partial-file review on the preview surface (the in-block note
        is invisible there)."""
        big = os.path.join(self.tmp, "big2.md")
        _write(big, "x" * (MAX_FILE_BYTES + 5000))
        small = os.path.join(self.tmp, "small2.md")
        _write(small, "y" * 50)
        _task, paths, truncated = compose(None, [big, small])
        self.assertEqual(paths, [big, small])
        self.assertEqual(truncated, [big], "only the oversize file is reported truncated")


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


class TestComposeErrorFraming(unittest.TestCase):
    """The single raised ConfigError is framed as a --doc failure, escape-safe, and
    never escapes as a foreign exception type (review folds: NUL traceback, header
    misattribution, preview-surface escape-safety)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def test_nul_byte_path_raises_config_error_not_value_error(self):
        """An embedded NUL in a --doc path makes open() raise ValueError; it must be
        caught and reframed, NOT escape as a traceback (the never-raise contract)."""
        with self.assertRaises(ConfigError) as ctx:
            compose("task", ["plan\x00.md"])
        # The clean message is produced; no ValueError escapes.
        self.assertIn("could not be read", str(ctx.exception))

    def test_doc_error_header_does_not_claim_invalid_fleet_config(self):
        """A --doc read failure must NOT be mislabeled 'Invalid fleet config:' — the
        fleet YAML is fine; the --doc path is the problem."""
        missing = os.path.join(self.tmp, "ghost.md")
        with self.assertRaises(ConfigError) as ctx:
            compose("task", [missing])
        msg = str(ctx.exception)
        self.assertNotIn("Invalid fleet config", msg)
        self.assertIn("--doc", msg)

    def test_error_message_is_escape_safe(self):
        """A --doc path carrying a terminal escape must not leak a raw control byte
        into the error message (it prints on the preview read-check surface). repr
        on both the path and the OSError filename keeps it inert."""
        evil = os.path.join(self.tmp, "plan\x1b[2Kevil.md")  # never created → read fails
        with self.assertRaises(ConfigError) as ctx:
            compose("task", [evil])
        self.assertNotIn("\x1b", str(ctx.exception), "raw ESC must not reach the surface")


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
                task, paths, _trunc = compose(None, ["~/notes.md"])
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
            task, _paths, _trunc = compose(None, [doc])
            self.assertIn("résumé — 日本語 — naïve", task)


if __name__ == "__main__":
    unittest.main()
