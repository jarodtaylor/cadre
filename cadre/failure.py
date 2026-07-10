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
    EMPTY_OUTPUT — the model call produced no usable text (a None/non-dict
                   result, or a missing/None/whitespace ``final_response``) with
                   CLEAN failure flags. Cadre's contract: no usable output is a
                   failure. This was the primary runtime reason in the chat()
                   era (U1 live spike, 2026-06-17: AIAgent logs a dead,
                   unauthorized, or unresolvable model and returns None rather
                   than raising); since #76 the adapter reads run_conversation's
                   structured flags, so flagged failures land on MODEL_ERROR and
                   only the genuinely-unmarked empty shapes land here. (The
                   taxonomy holds either way; only the distribution shifts.)
    MODEL_ERROR  — the model call failed with a signal: it raised an exception,
                   OR AIAgent's structured turn-outcome flags marked the turn
                   failed/interrupted/errored (#76 — e.g. the error-text-as-
                   response fall-through). AgentResult.error carries the raw
                   exception text or the structured flag detail alongside this
                   reason — never the response text.
    """

    OFF_PALETTE = "off_palette"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    EMPTY_OUTPUT = "empty_output"
    MODEL_ERROR = "model_error"
