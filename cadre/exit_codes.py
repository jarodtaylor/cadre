"""Centralized tri-state exit codes — the single source both runners map onto.

Both ``cadre run`` (``cadre/cli.py``) and the agent runner
(``cadre/data/skill/run.py``) return a process exit code; before this module
each runner computed it inline (``0 if result.ok else 1``), so the two could
silently drift onto different integers for the same outcome. ``ExitCode``
enumerates every code either runner returns, and ``status_to_exit`` is the one
mapping from a completed run's :class:`~cadre.engine.FleetStatus` to its exit
code (KTD3, #70) — both runners call it instead of inlining their own integer.

Caller-layer, not the engine: this module imports ``FleetStatus`` from
``cadre.engine`` (a plain data type, not engine behavior) but performs no I/O
of its own.
"""

from __future__ import annotations

from enum import IntEnum

from cadre.engine import FleetStatus


class ExitCode(IntEnum):
    """Every process exit code ``cadre run`` / the agent runner can return.

    SUCCESS          — the run produced full usable output (FleetStatus.SUCCESS).
    ERROR            — a generic pre-run/non-run error: an invalid fleet config,
                        a missing fleet file, a preview-bound approval that was
                        absent/mismatched/expired/wrong-flavored, a failure to
                        write an approval token, or a run-directory I/O error.
                        Named ERROR (not CONFIG_ERROR) because in the agent
                        runner this one code also covers the approval gate and
                        run-dir failures, not only a bad fleet config.
    USAGE            — a CLI usage error: neither --task nor --doc was given.
    DEGRADED         — the run produced a usable but partial result: every
                        specialist ran, but the convergence step (synthesis or
                        judge) failed (FleetStatus.DEGRADED). A distinct
                        non-zero code so an agent can tell "partial" from
                        "clean success" without reading the manifest.
    FAILED           — every specialist failed; convergence never ran
                        (FleetStatus.FAILED).
    PREFLIGHT_REFUSE — the #62 preflight gate refused an off-palette fleet
                        before any model call. Distinct from ERROR because a
                        preflight refusal writes no manifest, so the exit code
                        is its only structured signal.
    """

    SUCCESS = 0
    ERROR = 1
    USAGE = 2
    DEGRADED = 3
    FAILED = 4
    PREFLIGHT_REFUSE = 5


# The one status -> exit mapping. A run's status is always one of these three
# (FleetResult.status is never None and FleetResult.__post_init__ coerces any
# raw string to a FleetStatus member on construction), so an unrecognized key
# here would mean run_fleet returned a fourth status — fail loud (KeyError)
# rather than silently pick a code.
_STATUS_TO_EXIT: dict[FleetStatus, ExitCode] = {
    FleetStatus.SUCCESS: ExitCode.SUCCESS,
    FleetStatus.DEGRADED: ExitCode.DEGRADED,
    FleetStatus.FAILED: ExitCode.FAILED,
}


def status_to_exit(status: FleetStatus) -> int:
    """Map a completed run's tri-state status to its exit code.

    SUCCESS -> 0, DEGRADED -> 3, FAILED -> 4 — the one mapping both runners
    call instead of each inlining ``0 if result.ok else 1``.
    """
    return _STATUS_TO_EXIT[status]
