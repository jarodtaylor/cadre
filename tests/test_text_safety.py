"""Tests for fleet_engine.text_safety.sanitize (promoted from render._sanitize, GH #23).

Guards the behavior contract the whole trust surface depends on: control/escape
bytes stripped, bidi/line-sep stripped, legitimate content byte-identical, and
(new, GH #5 R4) a non-str value degrades to a safe placeholder instead of raising.
"""

import unittest

from fleet_engine.text_safety import BODY_GUTTER, frame_body, sanitize


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


    def test_strips_directional_marks(self):
        # ALM (U+061C), LRM (U+200E), RLM (U+200F) are directional format chars that
        # sit >= 0xA0 and were added to the denylist (CodeRabbit).
        self.assertEqual(sanitize("a\u061cb\u200ec\u200fd"), "abcd")


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

    def test_faulty_str_falls_back_to_placeholder_not_raise(self):
        # str() itself can raise on a hostile/broken __str__ — the never-raises
        # contract must still hold (Copilot).
        class Boom:
            def __str__(self):
                raise RuntimeError("boom")

        out = sanitize(Boom())
        self.assertIsInstance(out, str)  # no exception propagated

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


class TestSanitizeNonMultilineDropsAllLineBreakers(unittest.TestCase):
    """R5 (GH #45) — the inline bucket rests on non-multiline ``sanitize`` removing
    EVERY character a renderer treats as a line boundary, not just \\n/\\r. Stated as
    a property so a later refactor to explicit-char stripping can't reintroduce a gap.
    """

    def test_no_line_breaker_survives_non_multiline(self):
        # Everything < 0x20 (incl. VT/FF/FS/GS/RS), the C1 band 0x80-0x9F (incl. NEL
        # U+0085), and U+2028/U+2029 — none may open a new flush-left line.
        breakers = [
            "\n", "\r", "\x0b", "\x0c",  # LF CR VT FF
            "\x1c", "\x1d", "\x1e",  # FS GS RS
            "\x85",  # NEL (C1)
            "\u2028", "\u2029",  # LINE / PARAGRAPH SEPARATOR
        ]
        for ch in breakers:
            out = sanitize(f"a{ch}b")
            self.assertEqual(
                out, "ab", f"line-breaker U+{ord(ch):04X} survived non-multiline sanitize"
            )
            self.assertEqual(out.count("\n"), 0)


class TestFrameBody(unittest.TestCase):
    """GH #45 — frame_body gutters every model-body line so none is flush-left."""

    def test_single_line_first_line_guttered(self):
        # The first body line renders right after a ``--- role ---`` delimiter, so it
        # must be guttered too — the replace-on-newline idiom would skip it.
        self.assertEqual(frame_body("[ok  ] ghost (1/1)"), "│ [ok  ] ghost (1/1)")

    def test_every_line_including_blank_is_guttered(self):
        out = frame_body("a\n\nb")
        self.assertEqual(out, "│ a\n│ \n│ b")
        self.assertTrue(all(line.startswith(BODY_GUTTER) for line in out.split("\n")))

    def test_grammar_tokens_are_guttered_not_flush_left(self):
        body = (
            "--- provenance ---\n[FAIL] x\n# Specialist: ghost\n"
            "=== f — h ===\nnote: x\nNo judge grade"
        )
        for line in frame_body(body).split("\n"):
            self.assertTrue(line.startswith(BODY_GUTTER))
            self.assertFalse(line.startswith(("---", "[", "#", "===", "note", "No ")))

    def test_sanitizes_then_gutters_in_one_call(self):
        # An escape byte in the body is stripped AND the line guttered — one call.
        out = frame_body("clean\x1b[2Jmore")
        self.assertNotIn("\x1b", out)
        self.assertEqual(out, "│ clean[2Jmore")

    def test_gutter_opens_no_trusted_token(self):
        # The gutter itself must not begin like any R3 trusted flush-left row.
        for token in ("---", "[", "#", "===", "note", "No ", "⚠"):
            self.assertFalse(BODY_GUTTER.startswith(token))

    def test_empty_body_is_a_bare_gutter_line(self):
        # Cosmetic, pinned so it's a decision not an accident (GH #45 review).
        self.assertEqual(frame_body(""), BODY_GUTTER)


if __name__ == "__main__":
    unittest.main()
