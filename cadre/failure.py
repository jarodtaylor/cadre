"""Structured failure-reason taxonomy — shared by the preflight gate and every runtime lane failure.

A single ``FailureReason(str, Enum)`` spans two producers:

- ``OFF_PALETTE`` — the #62 preflight gate's refusal reason. It never lands on an
  ``AgentResult``: a preflight refusal happens before any lane is constructed, so
  this member names a pre-run refusal, not a lane outcome.
- ``TIMEOUT`` / ``SKIPPED`` / ``EMPTY_OUTPUT`` / ``MODEL_ERROR`` — the four ways a
  runtime model call can fail. Each is set at its failure's construction site,
  never inferred later by parsing ``notes`` / ``error`` text — see
  docs/solutions/design-patterns/populate-derived-field-eagerly-not-only-in-resolver.md.
  These are stamped on every failing model call's ``AgentResult`` (a specialist,
  the synthesizer, or the judge), but only the **per-lane (specialist)** reasons
  are surfaced downstream: the manifest ``lanes[]`` / ``rounds[][]`` records and
  the terminal render read ``result.specialists``. A synthesizer/judge failure is
  reported at the run level via ``synth_ok`` / ``judge_ok`` + a ``notes`` entry —
  its stamped reason is not surfaced (a run-level convergence reason is a possible
  #70 follow-up, not built here).

This module is a leaf: it imports nothing from ``cadre``, so ``model_client``,
``engine``, ``preflight``, ``capture``, and ``render`` can all import *from* it
with no import cycle. Lowercase string values match the ``FleetStatus`` precedent
(``cadre/engine.py``) so manifest serialization is stable across a str/enum
round-trip (docs/solutions/design-patterns/normalize-str-enum-at-the-boundary.md).
"""

from __future__ import annotations

from enum import Enum


class FailureReason(str, Enum):
    """Why a lane failed, or why a fleet was refused before any lane ran.

    OFF_PALETTE  — preflight refusal only (#62): a specialist/synthesizer/judge
                   model is absent from the resolved palette. Never set on an
                   AgentResult — the refusal happens before a lane exists.
    TIMEOUT      — the engine's outer wall-clock backstop fired on a wedged call.
    SKIPPED      — a sequential-chain lane the engine never ran because an
                   upstream lane failed.
    EMPTY_OUTPUT — the model call returned None/empty/whitespace text. Cadre's
                   contract: no usable output is a failure. In practice this is
                   the PRIMARY runtime failure reason, because AIAgent was
                   observed (U1 live spike, 2026-06-17) to return None rather
                   than raise on a dead, unauthorized, or unresolvable model —
                   so most real failures land here, not on MODEL_ERROR. (If a
                   future AIAgent version raised instead, they'd surface as
                   MODEL_ERROR; the taxonomy holds, only the distribution shifts.)
    MODEL_ERROR  — the model call raised an exception; the raw exception text
                   still lives in AgentResult.error alongside this reason.
    """

    OFF_PALETTE = "off_palette"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    EMPTY_OUTPUT = "empty_output"
    MODEL_ERROR = "model_error"
