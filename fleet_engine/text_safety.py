"""One sanitizing chokepoint for every external string a control/display surface prints.

Promoted from ``render._sanitize`` (GH #23): the function is now a shared trust
boundary imported across module lines (``render``, ``preview_lint``, ``capture``,
``cli``, and the ``cadre-fleet`` skill runner), so it lives in a small public
module rather than as an underscore-private helper in ``render``. Behavior is
unchanged from the original ``_sanitize``; the only addition is a non-``str``
guard so a malformed value degrades to a safe placeholder instead of raising on
a control surface (GH #5, R4).

Funnel every attacker-influenced string a control or display surface prints
through ``sanitize`` — piecemeal per-field patching guarantees the next reviewer
finds another hole.
"""

from __future__ import annotations

# Unicode line/paragraph separators and bidi format controls are never legitimate
# in a fleet field; >=0xA0 would otherwise pass them through and re-enable the
# fake-line / display-spoof the C0/C1 strip closes.
_UNSAFE_UNICODE = frozenset(
    "\u2028\u2029"                            # line / paragraph separators
    "\u202a\u202b\u202c\u202d\u202e"              # bidi embeddings / overrides (LRE..PDF, RLO)
    "\u2066\u2067\u2068\u2069"                    # bidi isolates (LRI, RLI, FSI, PDI)
    "\u061c\u200e\u200f"                          # directional marks (ALM, LRM, RLM)
)


def sanitize(text: object, *, multiline: bool = False) -> str:
    """Strip terminal-control characters from attacker-influenced text before display.

    A fleet YAML is attacker-controllable (library tampering — see the cadre-fleet
    SKILL.md Safety section), and its strings — and model output — flow into
    surfaces that are the operative human-okay control or a trusted record. An
    embedded ANSI/cursor escape sequence could otherwise overwrite or hide a
    printed warning (e.g. the privileged-tools line), spoofing the very output the
    human approves. Drop C0 controls (0x00–0x1F), DEL (0x7F), and C1 (0x80–0x9F):
    removing the ESC/CR/BS bytes defangs any sequence (a residual ``[2J`` then
    renders as inert text). Also drops Unicode line/paragraph separators (U+2028,
    U+2029) and bidi format controls (U+202A–U+202E, U+2066–U+2069), which >=0xA0
    would otherwise pass through and re-enable the fake-line / display-spoof the
    C0/C1 strip closes. Newlines survive only in multi-line content (a synthesis
    prompt, a report body); TAB is also preserved in multiline mode only;
    elsewhere both are dropped so a single-line field cannot inject a fake line.
    Printable Unicode (>= 0xA0) other than the excluded set passes through
    untouched, so a legitimate prompt renders byte-identically.

    Degrades, never raises (R4): a non-``str`` value (``None``, an int, a
    wrong-typed YAML value, or bytes) is coerced with ``str(...)`` so a control
    surface shows a coerced/placeholder string rather than tracebacking (even a
    hostile __str__ falls back to a placeholder). This is the
    fail-safe direction for a gate that must always render its go/no-go.
    """
    if not isinstance(text, str):
        # Malformed/wrong-typed input must not crash the surface. Coerce to a str
        # representation, then sanitize it like any other untrusted text — a
        # bytes object's repr, None, an int, etc. all become inert display text.
        # str() itself can raise on a hostile/broken __str__, so guard it: the
        # never-raises contract wins over faithfulness for a value that was already
        # the wrong type.
        try:
            text = str(text)
        except Exception:  # noqa: BLE001 -- never-raises contract (R4) intentionally catches all
            return "\ufffd"  # replacement char — a visible, inert placeholder
    return "".join(
        ch
        for ch in text
        if (
            (ch == "\n" and multiline)
            or (ch == "\t" and multiline)
            or (0x20 <= ord(ch) <= 0x7E)
            or ord(ch) >= 0xA0
        )
        and ch not in _UNSAFE_UNICODE
    )


# The gutter that frames a model-output body on a combined surface so no body line
# renders at column 0 and forges a trusted harness row (report-grammar mimicry, GH
# #45). A NON-space glyph is required: markdown ignores up to three leading spaces
# before ``#``/``---``, so a space indent would still let a body header forge on the
# ``.md`` surfaces; U+2502 is markdown-inert and opens no trusted grammar token.
BODY_GUTTER = "│ "  # U+2502 (box-drawing vertical) + a space


def frame_body(text: object, *, gutter: str = BODY_GUTTER) -> str:
    """Sanitize a model-output body and gutter every line so none renders at column 0.

    The one chokepoint for framing untrusted multiline model output on a combined
    surface (the terminal render; collect/judge ``synthesis.md``). It ``sanitize``s
    the body (multiline) then prefixes ``gutter`` to EVERY line — including the first,
    which renders immediately after a ``--- role ---`` delimiter, and blank lines — so
    the invariant "only trusted harness rows render at column 0" holds and is uniformly
    checkable (GH #45). Deliberately NOT ``text.replace(chr(10), chr(10) + gutter)``:
    that idiom skips the first line, leaving the delimiter-adjacent line flush-left and
    forgeable. Sanitizing internally makes one call per sink safe by construction —
    callers pass raw model text, not a pre-sanitized value.
    """
    body = sanitize(text, multiline=True)
    return "\n".join(gutter + line for line in body.split("\n"))
