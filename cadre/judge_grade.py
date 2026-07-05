"""Caller-layer parser: turns the judge's raw text into per-lane structure.

Caller-layer only: imported by ``render.py`` and ``capture.py``. NEVER imports
``cadre.engine`` — it operates on plain text and ``(role, model)``
tuples. The import-isolation contract mirrors ``file_input.py``
(``docs/solutions/architecture-patterns/side-effects-at-the-edge-pure-engine-core.md``).

``parse_grades`` leniently extracts per-lane entries from the judge's raw text
and matches each to a surviving lane on the EXACT ``role`` key (KTD9). It
never raises: total parse failure sets ``parsed_ok=False``, which signals
callers to fall back to the raw judge text (KTD2). Grade is preserved as a
string so prompt-determined forms (numeric, letter, pass/fail) pass through
verbatim (R7).

Format contract (pinned to ``_judge_prompt`` in ``engine.py``):

    === LANE: <exact role string> ===
    Grade: <grade value — judge's choice of scale>
    Rationale: <free-text justification; may span multiple lines>

Label keywords (``=== LANE:``) are matched case-insensitively; the captured
label is then matched to a surviving lane on EXACT case-sensitive ``role``
equality. A drifted label fails toward a false-partial (the lane lands in
``ungraded``) — never a false-full (KTD9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedGrades:
    """Structured outcome of parsing the judge's raw text.

    ``entries``: per-lane grade records, each a plain dict
    ``{role, model, grade, rationale}``; ``model`` is copied from the MATCHED
    surviving lane tuple, never from the judge's text.

    ``ungraded``: surviving lanes whose role did not appear as a parseable
    block header (or appeared with an empty grade) — they are flagged as
    ungraded. Partial coverage = ``parsed_ok is True and bool(ungraded)``.

    ``parsed_ok``: ``True`` when at least one entry was extracted. ``False``
    means the text yielded nothing usable; callers should fall back to the raw
    judge text (KTD2).
    """

    entries: list[dict]
    ungraded: list[tuple[str, str]]
    parsed_ok: bool


# ---------------------------------------------------------------------------
# Internal regex helpers
# ---------------------------------------------------------------------------

# The LANE marker regex is built per-call inside parse_grades from the per-run
# marker_nonce (R5, #5) — it is not a module constant because the nonce varies.
# Label is group(1), captured verbatim then .strip()ed; keyword (=== LANE:) is
# case-insensitive, the label itself is matched case-sensitively (KTD9).

# Matches the first "Grade:" field in a block body (rest of that line only).
# The value-gap is horizontal whitespace ONLY (`[^\S\n]*`, not `\s*`): a bare
# "Grade:" with the value on the next line must NOT let `(.+)` reach across the
# newline and capture the following "Rationale:" line as the grade — that would
# record a bogus grade AND mark an ungraded lane as graded (a false-full, the one
# direction KTD9 forbids). An empty grade value now matches nothing, so the lane
# correctly falls through to `ungraded` (false-partial). MULTILINE anchors ^ per-line.
_GRADE_RE = re.compile(r"^Grade:[^\S\n]*(.+)$", re.IGNORECASE | re.MULTILINE)

# Matches "Rationale:" and captures everything from there to the end of the
# block body string.  MULTILINE anchors ^ per-line; DOTALL lets .* cross
# newlines so multi-line rationales are captured whole.  The block body is
# already sliced to end at the next LANE marker (or end of judge_text), so
# the greedy .* never captures text from a subsequent lane.
_RATIONALE_RE = re.compile(r"^Rationale:\s*(.*)", re.IGNORECASE | re.DOTALL | re.MULTILINE)


def _parse_block(block_text: str) -> tuple[str, str]:
    """Extract grade and rationale from a single block body.

    Returns ``(grade, rationale)`` — both stripped strings (may be empty).
    Grade is preserved verbatim as a string (R7); rationale is everything
    after the ``Rationale:`` keyword to the end of the block.
    """
    grade = ""
    rationale = ""

    grade_m = _GRADE_RE.search(block_text)
    if grade_m:
        grade = grade_m.group(1).strip()

    rationale_m = _RATIONALE_RE.search(block_text)
    if rationale_m:
        rationale = rationale_m.group(1).strip()

    return grade, rationale


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_grades(
    judge_text: str,
    surviving_lanes: list[tuple[str, str]],
    marker_nonce: str | None,
) -> ParsedGrades:
    """Parse the judge's raw text into per-lane structured entries.

    ``surviving_lanes`` is the list of ``(role, model)`` the judge was asked
    to grade — callers build it as ``[(r.role, r.model) for r in result.successes]``.

    ``marker_nonce`` is the per-run token the engine embedded in every marker
    (``result.judge_marker_nonce``); a marker is recognized ONLY when it carries
    this exact nonce after the role (R5, #5). This is a cross-module format
    contract with ``engine._judge_prompt`` — the coupling test binds the two.
    A specialist never sees the judge prompt, so it cannot pre-plant the nonce;
    a ``=== LANE:`` it quotes (nonce-free) that the judge echoes is ignored,
    closing the single-injected-marker false-full. When ``marker_nonce`` is
    falsy (defensive — judge mode always sets it), no marker matches and the
    caller falls back to the raw judge text (KTD2).

    Matching rules (KTD9):

    - Each ``=== LANE: <label> <nonce> ===`` marker is matched to a surviving
      lane on the EXACT ``role`` string (case-sensitive). A drifted, paraphrased,
      or nonce-free label finds no match and is ignored.
    - A matched block without a non-empty grade is NOT turned into an entry
      (the lane lands in ``ungraded``).
    - A surviving lane with no matching block (or an empty-grade match) lands
      in ``ungraded``.
    - A block whose label matches no surviving lane is silently ignored —
      never invented as a new entry.  This is the fail-safe direction: a
      false-partial ("we flag a lane as ungraded") is always preferred over a
      false-full ("we hide a skipped lane") — KTD9.
    - ``model`` in each entry comes from the MATCHED LANE, never from the
      judge's text (the judge's text is untrusted provenance).

    Never raises; returns ``ParsedGrades(parsed_ok=False)`` on unparseable or
    empty input — the caller falls back to the raw judge text (KTD2).
    """
    # Build role -> (role, model) lookup from surviving lanes.
    # First occurrence wins if a role appears twice (degenerate input).
    role_to_lane: dict[str, tuple[str, str]] = {}
    for role, model in surviving_lanes:
        if role not in role_to_lane:
            role_to_lane[role] = (role, model)

    entries: list[dict] = []
    matched_roles: set[str] = set()

    # A marker is recognized ONLY when it carries the exact per-run nonce after the
    # role (R5, #5). Built per-call because the nonce varies per run — this is the
    # parser half of the format contract with engine._judge_prompt. A falsy nonce
    # (defensive; judge mode always sets one) matches nothing → fall back to raw text.
    # Case-insensitivity is scoped to the `LANE` keyword via `(?i:...)`; the nonce is
    # matched case-SENSITIVELY so a case-variant echo (ZZ9… for zz9…) cannot match a
    # lowercased-hex nonce (roles are also matched case-sensitively per KTD9).
    lane_re = (
        re.compile(rf"(?i:===\s*LANE:)\s*(.+?)\s+{re.escape(marker_nonce)}\s*===")
        if marker_nonce
        else None
    )

    if judge_text and lane_re is not None:
        matches = list(lane_re.finditer(judge_text))
        # Count markers per surviving role FIRST. A role whose `=== LANE: <role> <nonce> ===`
        # marker appears more than once is AMBIGUOUS and is left ungraded — there is no
        # safe way to tell the judge's real block from a duplicate. Picking the first would
        # let a repeated block forge a grade for a lane the judge graded once and report an
        # ambiguous result as graded — a false-FULL, the one direction KTD9 forbids.
        # Counting first turns that into a false-PARTIAL (the lane lands in `ungraded`).
        # Honest output names each lane once, so it is unaffected. The nonce closes the
        # single-injected-marker case: a specialist that embeds `=== LANE: <sibling> ===`
        # (which it cannot nonce, never having seen the judge prompt) and the judge echoes
        # is nonce-free, so lane_re does not match it at all.
        label_counts: dict[str, int] = {}
        for m in matches:
            lbl = m.group(1).strip()
            if lbl in role_to_lane:
                label_counts[lbl] = label_counts.get(lbl, 0) + 1

        for i, m in enumerate(matches):
            label = m.group(1).strip()
            block_start = m.end()
            block_end = matches[i + 1].start() if i + 1 < len(matches) else len(judge_text)
            block_body = judge_text[block_start:block_end]

            # Exact role match (case-sensitive — KTD9).
            if label not in role_to_lane:
                continue  # drifted or non-survivor label → ignore
            if label_counts[label] > 1:
                continue  # ambiguous: marker appears multiple times → leave ungraded
            if label in matched_roles:
                continue  # defensive (count==1 cannot repeat, but never double-add)

            grade, rationale = _parse_block(block_body)
            if not grade:
                continue  # entry requires a non-empty grade; lane → ungraded

            lane_role, lane_model = role_to_lane[label]
            entries.append({
                "role": lane_role,
                "model": lane_model,
                "grade": grade,
                "rationale": rationale,
            })
            matched_roles.add(label)

    ungraded = [(role, model) for role, model in surviving_lanes if role not in matched_roles]
    return ParsedGrades(
        entries=entries,
        ungraded=ungraded,
        parsed_ok=len(entries) > 0,
    )
