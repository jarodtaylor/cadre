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
    "  "                      # line / paragraph separators
    "‪‫‬‭‮"    # bidi embeddings / overrides
    "⁦⁧⁨⁩"          # bidi isolates
)


def sanitize(text: str, *, multiline: bool = False) -> str:
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
    surface shows a safe placeholder rather than tracebacking. This is the
    fail-safe direction for a gate that must always render its go/no-go.
    """
    if not isinstance(text, str):
        # Malformed/wrong-typed input must not crash the surface. Coerce to a str
        # representation, then sanitize it like any other untrusted text — a
        # bytes object's repr, None, an int, etc. all become inert display text.
        text = str(text)
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
