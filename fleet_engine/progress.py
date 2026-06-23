"""Pure progress-event vocabulary for a fleet run.

``run_fleet`` emits the lane/synthesis lifecycle events through a caller-injected
hook; the edge (``cli.py`` / ``run.py``) emits the validated/run-folder/completion
events it alone knows. ALL I/O — the stderr breadcrumbs, the heartbeat clock, the
incremental artifact writes — lives at the edge. The engine imports this module
but performs no I/O, so the suite stays hermetic
(``docs/solutions/architecture-patterns/side-effects-at-the-edge-pure-engine-core.md``).

Threading contract: the hook is always called from a SINGLE thread — the fan-out
drainer, then the main thread for synthesis — never from a worker thread. A hook
implementation therefore sees events serially and needs no lock of its own; the
only concurrent reader of edge state is the heartbeat timer, which the edge guards.

``LaneDone`` carries the whole ``AgentResult`` on purpose. The incremental capturer
is fed ONLY through this hook and must write a complete per-lane ``.md`` the moment
the lane finishes (R11) — a pure engine has no other channel to it, and the final
``FleetResult`` arrives too late. The stderr breadcrumb is what's restricted to the
safe label subset (role / outcome label / elapsed / resolved filename, never the raw
error string); that restriction lives in the renderer (U2), not in the event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Union

from fleet_engine.model_client import AgentResult

# ---------------------------------------------------------------------------
# Engine-emitted events — only run_fleet constructs these.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneLaunched:
    """Every specialist lane has been launched; carries their roles in config order."""

    roles: list[str]


@dataclass(frozen=True)
class LaneDone:
    """One specialist lane finished, carrying its full result.

    The capturer writes this lane's ``.md`` from ``result``; the renderer reads
    only ``result.role`` / outcome label / ``result.elapsed_s`` (plus the
    edge-resolved filename) — never ``result.error``.
    """

    result: AgentResult


@dataclass(frozen=True)
class SynthStarted:
    """Synthesis is about to run over the surviving lanes."""

    survivors: int


@dataclass(frozen=True)
class SynthDone:
    """Synthesis finished. ``outcome`` is a label (ok / failed / timed-out)."""

    outcome: str
    elapsed_s: float


# ---------------------------------------------------------------------------
# Edge-emitted events — cli.py / run.py construct these (the engine never does).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Validated:
    """The fleet validated and is about to run."""

    fleet: str
    specialists: int
    synthesizers: int = 1


@dataclass(frozen=True)
class RunFolder:
    """The run folder is reserved; artifacts will land here (capture on only)."""

    path: str


@dataclass(frozen=True)
class Completion:
    """The whole run finished. ``run_dir`` is None when capture is off (R13)."""

    elapsed_s: float
    run_dir: str | None = None


ProgressEvent = Union[
    LaneLaunched, LaneDone, SynthStarted, SynthDone, Validated, RunFolder, Completion
]
ProgressHook = Callable[[ProgressEvent], None]


def noop(event: ProgressEvent) -> None:
    """Default progress hook: drop every event.

    Lets ``run_fleet`` stay callable with no edge wiring, so existing callers and
    the hermetic test suite are unchanged.
    """
    return None


def outcome_label(result: AgentResult) -> str:
    """Map a collected result to a stable outcome label for breadcrumbs.

    ``timed-out`` takes precedence (a timed-out lane is also ``ok=False``), then
    ``ok``, else ``failed``. An internal label only — never the raw error string.
    """
    if result.timed_out:
        return "timed-out"
    return "ok" if result.ok else "failed"
