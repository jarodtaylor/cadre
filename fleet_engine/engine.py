"""Orchestration primitive: parallel fan-out -> synthesize.

The one primitive the MVP needs. Given a validated fleet config, a task, and a
model client, it runs each specialist concurrently, then synthesizes the
successful outputs with a strong model. It degrades gracefully — reporting
failures rather than crashing — and only fails outright when nothing usable
remains.

Every model call (each specialist and the synthesizer) runs in a daemon thread
under an outer wall-clock timeout, so a hung or pathologically slow provider can
neither stall the run nor block interpreter exit. This is a *backstop*, not the
primary cancel: AIAgent has its own per-request timeout (a stale-response detector
~90-120s plus a ~1800s client timeout) that raises into ``chat()`` and surfaces
here as a typed failure — that inner layer is what actually aborts the network call
and stops provider spend. The daemon timeout only bounds the run if that inner
layer wedges, and it is non-canceling: Python cannot kill a thread (a
``ThreadPoolExecutor`` worker is joined at shutdown and would re-hang the process; a
daemon thread is abandoned), so a timed-out lane's call may keep running briefly
until AIAgent's own timeout frees it — hence we never retry on a timeout.
``max_iterations`` bounds the agent loop and is a separate limit again.

Specialist completions are observed in ARRIVAL order: each lane's daemon pushes
``(index, result)`` onto a shared queue the instant its call returns, and a single
drainer pops them — so a slow lane never hides a fast one, and the live progress
counts stay honest (R4). A wedged lane never pushes; the drainer fabricates its
timed-out result and emits its lane-done itself after the deadline — single-source,
single-emit (a late-returning abandoned worker's queue put is simply never drained).

The engine stays a pure event source: it emits lifecycle events through an
optional caller-injected ``progress`` hook (default no-op) and performs no I/O —
no ``print``/``open``/``stderr``. The edge renders breadcrumbs and writes artifacts.

The engine holds no fleet-domain strings and no AIAgent knowledge: it depends on
``FleetConfig`` (data) and ``ModelClient`` (behavior), both injectable, so every
path is testable against a fake with no live calls.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from fleet_engine.config import FleetConfig, SpecialistSpec
from fleet_engine.model_client import AgentResult, ModelClient
from fleet_engine.progress import (
    LaneDone,
    LaneLaunched,
    ProgressHook,
    SynthDone,
    SynthStarted,
    noop,
    outcome_label,
)

# Outer wall-clock backstop for any single model call. NOT the primary cancel:
# AIAgent's own per-request timeout (a stale-response detector ~90-120s plus a
# ~1800s client timeout) is what aborts a hung call and stops provider spend; this
# only bounds run_fleet if that inner layer wedges. Generous on purpose — well above
# the inner detector and above a typical multi-iteration lane, so it fires on a true
# wedge, not on healthy-but-slow work. Tune against live host numbers (a maximally
# deep lane, max_iterations~90, can approach this). Override via
# run_fleet(call_timeout=...); None disables it (block until every call returns).
DEFAULT_CALL_TIMEOUT = 600.0


@dataclass
class FleetResult:
    fleet: str
    task: str
    specialists: list[AgentResult]                   # every specialist, success or failure (provenance)
    synthesis: str | None = None                     # synthesized text, or None if synthesis didn't happen
    notes: list[str] = field(default_factory=list)   # failure / degradation notes
    ok: bool = False                                 # True only when a synthesis was produced
    synth_ok: bool | None = None                     # None=not attempted; True=succeeded; False=ran+failed

    @property
    def successes(self) -> list[AgentResult]:
        return [r for r in self.specialists if r.ok]

    @property
    def failures(self) -> list[AgentResult]:
        return [r for r in self.specialists if not r.ok]


def _specialist_prompt(spec: SpecialistSpec, task: str) -> str:
    focus = f"\nFocus: {spec.focus}" if spec.focus else ""
    return f"You are the '{spec.role}' specialist.{focus}\n\nTask: {task}"


def _synthesis_prompt(config: FleetConfig, task: str, successes: list[AgentResult]) -> str:
    base = config.synthesis.prompt or (
        "Synthesize the specialist findings into one grounded report on the task. "
        "Attribute each claim to the specialist that surfaced it and preserve citations."
    )
    findings = "\n\n".join(f"--- {r.role} (model: {r.model}) ---\n{r.text}" for r in successes)
    return f"{base}\n\nTask: {task}\n\nSpecialist findings:\n{findings}"


def _start_daemon(fn: Callable[[], AgentResult], name: str) -> tuple[threading.Thread, list[AgentResult], float]:
    """Run ``fn`` in a daemon thread; return (thread, holder, launched_at).

    Daemon so a hung provider can never block interpreter exit. ``fn`` (a
    ``ModelClient.run`` call) does not raise by contract, so its result lands in
    ``holder`` when the call returns; the caller joins with a deadline and reads
    an empty holder as a timeout. ``launched_at`` is ``time.monotonic()`` captured
    at thread start — used by ``_collect`` to compute ``elapsed_s`` for every lane.
    """
    holder: list[AgentResult] = []
    thread = threading.Thread(target=lambda: holder.append(fn()), name=name, daemon=True)
    thread.start()
    launched_at = time.monotonic()
    return thread, holder, launched_at


def _start_lane(
    idx: int,
    fn: Callable[[], AgentResult],
    done_q: "queue.Queue[tuple[int, AgentResult]]",
    name: str,
) -> float:
    """Launch ``fn`` in a daemon thread that pushes ``(idx, result)`` onto ``done_q``
    the instant the call returns; return its launch time (``time.monotonic()``).

    Daemon so a wedged provider can never block interpreter exit; the queue push is
    how the drainer observes completion in ARRIVAL order (a slow lane never hides a
    fast one). ``idx`` and ``fn`` are passed as args (a fresh frame per lane), so each
    thread closes over its own lane, not the loop's last. ``fn`` (a ``ModelClient.run``
    call) does not raise by contract; if it ever did, the put never happens and the
    drainer reads the silent lane as a timeout — the same degradation as ``_collect``.
    """
    threading.Thread(target=lambda: done_q.put((idx, fn())), name=name, daemon=True).start()
    return time.monotonic()


def _collect(
    started: tuple[threading.Thread, list[AgentResult], float],
    deadline: float | None,
    role: str,
    provider: str,
    model: str,
    timeout: float | None,
    toolset: list[str],
) -> AgentResult:
    """Join until ``deadline``; return the call's result, or a typed timeout failure.

    The timeout failure is built lazily — only on a real timeout, which cannot
    happen when ``deadline`` is None — so ``timeout`` is always a number here.

    Enriches the returned result uniformly across all paths: ``elapsed_s`` is set
    from the daemon's launch time, ``toolset`` is the validated config toolset (passed
    verbatim — never coerced through a truthiness check; [] means no tools), and
    ``timed_out`` is True only on the fabricated timeout result.
    """
    thread, holder, launched_at = started
    thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
    now = time.monotonic()
    if holder:
        result = holder[0]
        # The engine is the single source of truth for timed_out: a returned
        # call did not time out, regardless of what the client left on the field.
        result.timed_out = False
    else:
        result = AgentResult(role=role, provider=provider, model=model, ok=False,
                             error=f"timed out after {timeout:g}s", timed_out=True)
    result.elapsed_s = now - launched_at
    result.toolset = list(toolset)
    return result


def _specialist_call(client: ModelClient, spec: SpecialistSpec, task: str) -> Callable[[], AgentResult]:
    # A fresh frame per spec so the thunk closes over THIS spec, not the loop's
    # last one — the threads run concurrently with the fan-out loop.
    return lambda: client.run(
        role=spec.role,
        provider=spec.provider,
        model=spec.model,
        toolset=spec.toolset,
        prompt=_specialist_prompt(spec, task),
    )


def _fan_out(
    config: FleetConfig,
    task: str,
    client: ModelClient,
    call_timeout: float | None,
    progress: ProgressHook,
) -> list[AgentResult]:
    """Run every specialist concurrently; return results in CONFIG order.

    Each lane's daemon pushes ``(idx, result)`` onto ``done_q`` on arrival; this
    thread drains them, stamps the capture signals, and emits one ``LaneDone`` per
    lane in ARRIVAL order — so a slow lane never hides a fast one and the live counts
    stay honest (R4). Lanes still running at the shared deadline are fabricated as
    timed-out here (single-emit — the queue is never read again, so a late-returning
    abandoned worker can't re-fire). The returned list is CONFIG-ordered: capture
    dedup and the manifest depend on that stable order, independent of arrival.

    All emission happens from THIS thread — never a worker — so the hook sees events
    serially and the edge guards only the heartbeat.
    """
    n = len(config.specialists)
    done_q: "queue.Queue[tuple[int, AgentResult]]" = queue.Queue()
    # Start every lane before draining any, so they run concurrently under one shared
    # deadline (total ~= call_timeout, not N x call_timeout).
    launched_at = [
        _start_lane(idx, _specialist_call(client, spec, task), done_q, name=f"fleet-{spec.role}")
        for idx, spec in enumerate(config.specialists)
    ]
    progress(LaneLaunched(roles=[spec.role for spec in config.specialists]))

    deadline = None if call_timeout is None else time.monotonic() + call_timeout
    collected: dict[int, AgentResult] = {}
    while len(collected) < n:
        if deadline is None:
            idx, lane = done_q.get()  # block until every lane arrives (call_timeout=None)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                idx, lane = done_q.get(timeout=remaining)
            except queue.Empty:
                break
        # Stamp capture signals BEFORE emitting, so the captured .md / event is
        # complete. The engine is the single source of truth for these (a returned
        # call did not time out, regardless of what the client left on the field).
        lane.timed_out = False
        lane.elapsed_s = time.monotonic() - launched_at[idx]
        lane.toolset = list(config.specialists[idx].toolset)
        collected[idx] = lane
        progress(LaneDone(result=lane))

    # Lanes still unseen at the deadline wedged: the drainer fabricates and emits
    # their lane-done itself. (``call_timeout`` is a number whenever this runs — a
    # None timeout never breaks out of the loop above, so the format is safe.)
    fabricated_at = time.monotonic()
    for idx, spec in enumerate(config.specialists):
        if idx in collected:
            continue
        timed = AgentResult(
            role=spec.role, provider=spec.provider, model=spec.model, ok=False,
            error=f"timed out after {call_timeout:g}s", timed_out=True,
        )
        timed.elapsed_s = fabricated_at - launched_at[idx]
        timed.toolset = list(spec.toolset)
        collected[idx] = timed
        progress(LaneDone(result=timed))

    return [collected[i] for i in range(n)]


def run_fleet(
    config: FleetConfig,
    task: str,
    client: ModelClient,
    *,
    call_timeout: float | None = DEFAULT_CALL_TIMEOUT,
    progress: ProgressHook = noop,
) -> FleetResult:
    """Run the fleet on a task and return a provenance-tagged result.

    ``call_timeout`` is the per-call wall-clock ceiling (seconds) applied to each
    specialist and the synthesizer; None disables it. ``progress`` is an optional
    lifecycle hook (default no-op): the engine emits pure events through it and does
    no I/O of its own — the edge renders breadcrumbs and writes artifacts.
    """
    specialist_results = _fan_out(config, task, client, call_timeout, progress)

    result = FleetResult(fleet=config.name, task=task, specialists=specialist_results)
    for failed in result.failures:
        result.notes.append(f"specialist '{failed.role}' failed: {failed.error}")

    successes = result.successes
    if not successes:
        result.notes.append("all specialists failed — no synthesis")
        return result  # ok stays False; synthesis never runs, so no synth-started

    if len(successes) == 1:
        result.notes.append("synthesized from a single surviving specialist (degenerate fan-out)")

    progress(SynthStarted(survivors=len(successes)))

    # Synthesize over the survivors with the strong model — also timed, in a daemon
    # thread. A hung synthesizer would otherwise stall the main thread with nothing
    # to abandon (worse than a stuck specialist: the process could not even exit).
    synth_started = _start_daemon(
        lambda: client.run(
            role="synthesizer",
            provider=config.synthesis.provider,
            model=config.synthesis.model,
            prompt=_synthesis_prompt(config, task, successes),
        ),
        name="fleet-synthesizer",
    )
    synth_deadline = None if call_timeout is None else time.monotonic() + call_timeout
    synth = _collect(
        synth_started, synth_deadline,
        "synthesizer", config.synthesis.provider, config.synthesis.model, call_timeout,
        toolset=[],  # synthesizer has no configured toolset; [] = fail-closed zero tools
    )
    result.synth_ok = synth.ok
    if synth.ok:
        result.synthesis = synth.text
        result.ok = True
    else:
        # Synthesizer failed (error or timeout): return the labeled specialist
        # outputs plus a note, no synthesized text. Still a usable partial result
        # that honors R9.
        result.notes.append(f"synthesizer failed: {synth.error}")

    progress(SynthDone(outcome=outcome_label(synth), elapsed_s=synth.elapsed_s or 0.0))

    # Seam (R12): an independent-critic stage composes here — take this
    # FleetResult, add a critique/confidence score — without touching the
    # fan-out/synthesize path above. Not built in the MVP.
    return result
