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
    ok: bool = False                                 # True only when a synthesis was produced (synthesize) or >=1 specialist succeeded (collect)
    synth_ok: bool | None = None                     # None=not attempted; True=succeeded; False=ran+failed
    convergence: str = "synthesize"                  # "synthesize" or "collect" — always set; consumers must read this to disambiguate ok=True,synthesis=None

    @property
    def successes(self) -> list[AgentResult]:
        return [r for r in self.specialists if r.ok]

    @property
    def failures(self) -> list[AgentResult]:
        return [r for r in self.specialists if not r.ok]


def _specialist_prompt(spec: SpecialistSpec, task: str) -> str:
    focus = f"\nFocus: {spec.effective_instruction}" if spec.effective_instruction else ""
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
    done_q: "queue.Queue[tuple[int, AgentResult, float]]",
    name: str,
) -> float:
    """Launch ``fn`` in a daemon thread that pushes ``(idx, result, completed_at)``
    onto ``done_q`` the instant the call returns; return its launch time.

    Daemon so a wedged provider can never block interpreter exit; the queue push is
    how the drainer observes completion in ARRIVAL order (a slow lane never hides a
    fast one). ``completed_at`` is the worker's OWN ``time.monotonic()`` at push, so
    the drainer can compute ``elapsed_s`` and decide timed-out-or-not from when the
    lane *actually finished* — never from when a (possibly slow) progress hook let
    the drainer get around to pulling it. ``idx`` and ``fn`` are passed as args (a
    fresh frame per lane), so each thread closes over its own lane, not the loop's
    last. ``fn`` (a ``ModelClient.run`` call) does not raise by contract; if it ever
    did, the put never happens and the drainer reads the silent lane as a timeout.
    """

    # Capture launch time BEFORE starting the thread: with an instant ``fn`` the worker
    # can run and stamp ``completed_at`` before this function would otherwise reach a
    # post-start ``monotonic()``, which would make ``completed_at - launched_at`` go
    # NEGATIVE. Captured first, ``launched_at <= completed_at`` always holds.
    launched_at = time.monotonic()

    def _run() -> None:
        result = fn()
        done_q.put((idx, result, time.monotonic()))

    threading.Thread(target=_run, name=name, daemon=True).start()
    return launched_at


def _timed_out_result(role: str, provider: str, model: str, timeout: float) -> AgentResult:
    """Build the fabricated timeout failure for a wedged call.

    The single place the ``timed out after {t:g}s`` error string is constructed —
    shared by the synth join (``_collect``) and the specialist drainer (``_fan_out``),
    which both fabricate this result for a call that never returned by the deadline.
    ``elapsed_s`` and ``toolset`` are stamped by the caller (they differ per lane).
    """
    return AgentResult(
        role=role, provider=provider, model=model, ok=False,
        error=f"timed out after {timeout:g}s", timed_out=True,
    )


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
        result = _timed_out_result(role, provider, model, timeout)
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

    Each lane's daemon pushes ``(idx, result, completed_at)`` onto ``done_q`` on
    arrival; this thread drains them, stamps the capture signals, and emits one
    ``LaneDone`` per lane in ARRIVAL order — so a slow lane never hides a fast one and
    the live counts stay honest (R4). The returned list is CONFIG-ordered: capture
    dedup and the manifest depend on that stable order, independent of arrival.

    Timeout accounting is decoupled from progress-hook latency. The production hook
    does stderr rendering AND synchronous per-lane capture I/O, which can be slow; if
    the deadline were re-checked only between hook calls, a slow hook on one lane
    could let the deadline lapse while *already-finished* results sit unread in the
    queue, fabricating those completed lanes as false timeouts (cross-model
    adversarial finding). So a lane is a timeout iff it NEVER pushed: Phase 1 waits up
    to the shared deadline; Phase 2 then non-blockingly drains the backlog — any lane
    that pushed completed in time, even if a slow hook delayed the pull; only Phase 3
    (never-pushed lanes) fabricates timeouts (single-emit — a late push is never
    drained). ``elapsed_s`` uses the worker's own ``completed_at``, so hook latency
    can't inflate it either.

    All emission happens from THIS thread — never a worker — so the hook sees events
    serially and the edge guards only the heartbeat.
    """
    n = len(config.specialists)
    done_q: "queue.Queue[tuple[int, AgentResult, float]]" = queue.Queue()
    # Start every lane before draining any, so they run concurrently under one shared
    # deadline (total ~= call_timeout, not N x call_timeout).
    launched_at = [
        _start_lane(idx, _specialist_call(client, spec, task), done_q, name=f"fleet-{spec.role}")
        for idx, spec in enumerate(config.specialists)
    ]
    progress(LaneLaunched(roles=[spec.role for spec in config.specialists]))

    collected: dict[int, AgentResult] = {}

    def _record(idx: int, lane: AgentResult, completed_at: float) -> None:
        # Stamp from the worker's OWN completion time (not the drain time), then emit
        # — so the captured .md/event is complete and a slow hook can't skew elapsed.
        # The engine is the single source of truth: a pushed result did not time out,
        # regardless of what the client left on the field.
        lane.timed_out = False
        lane.elapsed_s = completed_at - launched_at[idx]
        lane.toolset = list(config.specialists[idx].toolset)
        collected[idx] = lane
        progress(LaneDone(result=lane))

    deadline = None if call_timeout is None else time.monotonic() + call_timeout
    # Phase 1 — wait for arrivals up to the shared deadline, emitting in arrival order.
    while len(collected) < n:
        if deadline is None:
            idx, lane, completed_at = done_q.get()  # block until all arrive (call_timeout=None)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                idx, lane, completed_at = done_q.get(timeout=remaining)
            except queue.Empty:
                break
        _record(idx, lane, completed_at)

    # Phase 2 — drain the backlog of lanes that already PUSHED a result but weren't
    # pulled (a slow Phase-1 hook can let the deadline lapse while finished results
    # queue up). These completed in time, so they are successes, NOT timeouts. This is
    # the fix for the hook-latency false-timeout bug. get_nowait is non-blocking, so a
    # genuinely wedged lane (never pushed) just isn't here and falls to Phase 3.
    while len(collected) < n:
        try:
            idx, lane, completed_at = done_q.get_nowait()
        except queue.Empty:
            break
        _record(idx, lane, completed_at)

    # Phase 3 — lanes that never pushed are genuinely wedged: the drainer fabricates
    # and emits their timed-out lane-done. (``call_timeout`` is a number whenever this
    # fabricates — a None timeout blocks in Phase 1 until all arrive, so n are
    # collected and this loop is a no-op; the format is safe.)
    fabricated_at = time.monotonic()
    for idx, spec in enumerate(config.specialists):
        if idx in collected:
            continue
        timed = _timed_out_result(spec.role, spec.provider, spec.model, call_timeout)
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

    result = FleetResult(fleet=config.name, task=task, specialists=specialist_results, convergence=config.convergence)
    for failed in result.failures:
        result.notes.append(f"specialist '{failed.role}' failed: {failed.error}")

    successes = result.successes
    if not successes:
        result.notes.append("all specialists failed — no synthesis")
        return result  # ok stays False; synthesis never runs, so no synth-started

    if config.convergence == "collect":
        # Collect: return raw specialist lanes; synthesizer is never invoked.
        # ok = True here (past the all-fail guard, so >=1 specialist succeeded).
        # synthesis stays None, synth_ok stays None — read result.convergence to
        # distinguish from an all-failed synthesize run (KTD2).
        result.ok = True  # guaranteed past the all-fail guard above (>=1 specialist succeeded)
        return result

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
