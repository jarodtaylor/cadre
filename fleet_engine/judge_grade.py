"""Caller-layer parser: turns the judge's raw text into per-lane structure.

Caller-layer only: imported by ``render.py`` and ``capture.py``. NEVER imports
``fleet_engine.engine`` — it operates on plain text and ``(role, model)``
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

# Matches a LANE marker.  Label is group(1), captured verbatim then .strip()ed.
# Keyword keywords (=== LANE:) are case-insensitive; the label itself is not
# touched — matching happens on the raw stripped label (case-sensitive, KTD9).
_LANE_RE = re.compile(r"===\s*LANE:\s*(.+?)\s*===", re.IGNORECASE)

# Matches the first "Grade:" field in a block body (rest of that line only —
# no DOTALL so .+ stops at the newline).  MULTILINE so ^ anchors per-line.
_GRADE_RE = re.compile(r"^Grade:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

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


def parse_grades(judge_text: str, surviving_lanes: list[tuple[str, str]]) -> ParsedGrades:
    """Parse the judge's raw text into per-lane structured entries.

    ``surviving_lanes`` is the list of ``(role, model)`` the judge was asked
    to grade — callers build it as ``[(r.role, r.model) for r in result.successes]``.

    Matching rules (KTD9):

    - Each ``=== LANE: <label> ===`` marker is matched to a surviving lane on
      the EXACT ``role`` string (case-sensitive). A drifted or paraphrased
      label finds no match and is ignored.
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

    if judge_text:
        matches = list(_LANE_RE.finditer(judge_text))
        for i, m in enumerate(matches):
            label = m.group(1).strip()
            block_start = m.end()
            block_end = matches[i + 1].start() if i + 1 < len(matches) else len(judge_text)
            block_body = judge_text[block_start:block_end]

            # Exact role match (case-sensitive — KTD9).
            if label not in role_to_lane:
                continue  # drifted or non-survivor label → ignore

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
