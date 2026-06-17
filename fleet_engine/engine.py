"""Orchestration primitive: parallel fan-out -> synthesize.

The one primitive the MVP needs. Given a validated fleet config, a task, and a
model client, it runs each specialist concurrently, then synthesizes the
successful outputs with a strong model. It degrades gracefully — reporting
failures rather than crashing — and only fails outright when nothing usable
remains.

The engine holds no fleet-domain strings and no AIAgent knowledge: it depends on
``FleetConfig`` (data) and ``ModelClient`` (behavior), both injectable, so every
path is testable against a fake with no live calls.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from fleet_engine.config import FleetConfig, SpecialistSpec
from fleet_engine.model_client import AgentResult, ModelClient


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


def run_fleet(
    config: FleetConfig,
    task: str,
    client: ModelClient,
    *,
    max_workers: int | None = None,
) -> FleetResult:
    """Run the fleet on a task and return a provenance-tagged result."""
    # Fan out: one ephemeral agent per specialist, concurrently.
    workers = max_workers or max(1, len(config.specialists))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        specialist_results = list(
            pool.map(
                lambda spec: client.run(
                    role=spec.role,
                    provider=spec.provider,
                    model=spec.model,
                    toolset=spec.toolset,
                    prompt=_specialist_prompt(spec, task),
                ),
                config.specialists,
            )
        )

    result = FleetResult(fleet=config.name, task=task, specialists=specialist_results)
    for failed in result.failures:
        result.notes.append(f"specialist '{failed.role}' failed: {failed.error}")

    successes = result.successes
    if not successes:
        result.notes.append("all specialists failed — no synthesis")
        return result  # ok stays False

    if len(successes) == 1:
        result.notes.append("synthesized from a single surviving specialist (degenerate fan-out)")

    # Synthesize over the survivors with the strong model.
    synth = client.run(
        role="synthesizer",
        provider=config.synthesis.provider,
        model=config.synthesis.model,
        prompt=_synthesis_prompt(config, task, successes),
    )
    if synth.ok:
        result.synthesis = synth.text
        result.ok = True
    else:
        # Synthesizer failed: return the labeled specialist outputs plus a note,
        # no synthesized text. Still a usable partial result that honors R9.
        result.notes.append(f"synthesizer failed: {synth.error}")

    # Seam (R12): an independent-critic stage composes here — take this
    # FleetResult, add a critique/confidence score — without touching the
    # fan-out/synthesize path above. Not built in the MVP.
    return result
