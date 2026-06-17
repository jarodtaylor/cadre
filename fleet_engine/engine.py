"""Orchestration primitive: parallel fan-out -> synthesize.

The one primitive the MVP needs. Given a validated fleet config, a task, and a
model client, it runs each specialist concurrently, then synthesizes the
successful outputs with a strong model. It degrades gracefully — reporting
failures rather than crashing — and only fails outright when nothing usable
remains.

Every model call (each specialist and the synthesizer) runs in a daemon thread
under a wall-clock timeout, so a hung or pathologically slow provider can neither
stall the run nor block interpreter exit. Python cannot kill a thread: a
``ThreadPoolExecutor`` worker is joined at shutdown and would re-hang the process,
whereas a daemon thread is abandoned. ``AIAgent``'s ``max_iterations`` bounds the
agent loop, not wall-clock on a stuck network call — this timeout is that bound.

The engine holds no fleet-domain strings and no AIAgent knowledge: it depends on
``FleetConfig`` (data) and ``ModelClient`` (behavior), both injectable, so every
path is testable against a fake with no live calls.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from fleet_engine.config import FleetConfig, SpecialistSpec
from fleet_engine.model_client import AgentResult, ModelClient

# Wall-clock ceiling for any single model call. A safety net against a hung or
# pathologically slow provider — generous on purpose, so it fires only on a
# genuine hang, never on a healthy-but-slow lane. Override via
# run_fleet(call_timeout=...); None disables it (block until every call returns).
DEFAULT_CALL_TIMEOUT = 300.0


@dataclass
class FleetResult:
    fleet: str
    task: str
    specialists: list[AgentResult]                   # every specialist, success or failure (provenance)
    synthesis: str | None = None                     # synthesized text, or None if synthesis didn't happen
    notes: list[str] = field(default_factory=list)   # failure / degradation notes
    ok: bool = False                                 # True only when a synthesis was produced

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


def _start_daemon(fn: Callable[[], AgentResult], name: str) -> tuple[threading.Thread, list[AgentResult]]:
    """Run ``fn`` in a daemon thread; return (thread, holder).

    Daemon so a hung provider can never block interpreter exit. ``fn`` (a
    ``ModelClient.run`` call) does not raise by contract, so its result lands in
    ``holder`` when the call returns; the caller joins with a deadline and reads
    an empty holder as a timeout.
    """
    holder: list[AgentResult] = []
    thread = threading.Thread(target=lambda: holder.append(fn()), name=name, daemon=True)
    thread.start()
    return thread, holder


def _collect(
    started: tuple[threading.Thread, list[AgentResult]],
    deadline: float | None,
    role: str,
    provider: str,
    model: str,
    timeout: float | None,
) -> AgentResult:
    """Join until ``deadline``; return the call's result, or a typed timeout failure.

    The timeout failure is built lazily — only on a real timeout, which cannot
    happen when ``deadline`` is None — so ``timeout`` is always a number here.
    """
    thread, holder = started
    thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
    if holder:
        return holder[0]
    return AgentResult(role=role, provider=provider, model=model, ok=False,
                       error=f"timed out after {timeout:g}s")


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


def run_fleet(
    config: FleetConfig,
    task: str,
    client: ModelClient,
    *,
    call_timeout: float | None = DEFAULT_CALL_TIMEOUT,
) -> FleetResult:
    """Run the fleet on a task and return a provenance-tagged result.

    ``call_timeout`` is the per-call wall-clock ceiling (seconds) applied to each
    specialist and the synthesizer; None disables it.
    """
    # Fan out: one ephemeral agent per specialist, each in its own daemon thread.
    # Start them all before joining any, so they run concurrently under a single
    # shared deadline (total ~= call_timeout, not N x call_timeout).
    started = [
        (spec, _start_daemon(_specialist_call(client, spec, task), name=f"fleet-{spec.role}"))
        for spec in config.specialists
    ]
    deadline = None if call_timeout is None else time.monotonic() + call_timeout
    specialist_results = [
        _collect(handle, deadline, spec.role, spec.provider, spec.model, call_timeout)
        for spec, handle in started
    ]

    result = FleetResult(fleet=config.name, task=task, specialists=specialist_results)
    for failed in result.failures:
        result.notes.append(f"specialist '{failed.role}' failed: {failed.error}")

    successes = result.successes
    if not successes:
        result.notes.append("all specialists failed — no synthesis")
        return result  # ok stays False

    if len(successes) == 1:
        result.notes.append("synthesized from a single surviving specialist (degenerate fan-out)")

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
    )
    if synth.ok:
        result.synthesis = synth.text
        result.ok = True
    else:
        # Synthesizer failed (error or timeout): return the labeled specialist
        # outputs plus a note, no synthesized text. Still a usable partial result
        # that honors R9.
        result.notes.append(f"synthesizer failed: {synth.error}")

    # Seam (R12): an independent-critic stage composes here — take this
    # FleetResult, add a critique/confidence score — without touching the
    # fan-out/synthesize path above. Not built in the MVP.
    return result
