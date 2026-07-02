"""Tests for fleet_engine.text_safety.sanitize (promoted from render._sanitize, GH #23).

Guards the behavior contract the whole trust surface depends on: control/escape
bytes stripped, bidi/line-sep stripped, legitimate content byte-identical, and
(new, GH #5 R4) a non-str value degrades to a safe placeholder instead of raising.
"""

import unittest

from fleet_engine.text_safety import sanitize


class TestSanitizeStripsControlBytes(unittest.TestCase):
    def test_strips_c0_controls_and_esc(self):
        # An ANSI clear-screen / cursor-move sequence loses its ESC byte, so the
        # residual renders as inert text — it can no longer move the cursor.
        out = sanitize("clean\x1b[2Jmore\x00\x07end")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x00", out)
        self.assertNotIn("\x07", out)
        self.assertEqual(out, "clean[2Jmoreend")

    def test_strips_del_and_c1(self):
        self.assertEqual(sanitize("a\x7fb\x9fc"), "abc")

    def test_strips_bidi_and_line_separators(self):
        # U+2028 line sep, U+202E RLO, U+2066 LRI all sit >= 0xA0 and would sail
        # through a naive "keep >= 0xA0" allowance — they must be excluded.
        self.assertEqual(sanitize("a b‮c⁦d"), "abcd")


class TestSanitizePreservesLegitimateContent(unittest.TestCase):
    def test_printable_ascii_and_unicode_byte_identical(self):
        s = "Hello — a normal prompt with “curly quotes”, em-dash, café, 日本語."
        self.assertEqual(sanitize(s), s)

    def test_newline_and_tab_dropped_single_line(self):
        # Single-line mode drops \n and \t so a field cannot inject a fake line.
        self.assertEqual(sanitize("line1\nline2\tcol"), "line1line2col")

    def test_newline_and_tab_preserved_multiline(self):
        self.assertEqual(
            sanitize("para1\n\npara2\twith tab", multiline=True),
            "para1\n\npara2\twith tab",
        )

    def test_multiline_still_strips_control_bytes(self):
        self.assertEqual(sanitize("a\rb\x1bc", multiline=True), "abc")


class TestSanitizeDegradesNeverRaises(unittest.TestCase):
    """R4 — a wrong-typed value on a control surface must not traceback."""

    def test_none_coerces_not_raises(self):
        self.assertEqual(sanitize(None), "None")

    def test_int_coerces(self):
        self.assertEqual(sanitize(42), "42")

    def test_list_coerces_and_is_sanitized(self):
        # A wrong-typed YAML value (list where a string was expected) renders as
        # its inert repr rather than crashing the surface.
        self.assertEqual(sanitize(["a", "b"]), "['a', 'b']")

    def test_bytes_with_embedded_nul_coerces_without_raising(self):
        # bytes -> repr string; no NUL survives, no exception.
        out = sanitize(b"ab\x00cd")
        self.assertNotIn("\x00", out)
        self.assertIsInstance(out, str)


class TestSanitizeAliasedFromRender(unittest.TestCase):
    def test_render_private_alias_is_the_same_callable(self):
        # render._sanitize must remain a working alias so its in-file call sites
        # and any legacy importer keep working after the #23 move.
        from fleet_engine.render import _sanitize as render_sanitize

        self.assertIs(render_sanitize, sanitize)


if __name__ == "__main__":
    unittest.main()
