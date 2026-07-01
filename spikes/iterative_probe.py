#!/usr/bin/env python
"""Iterative-topology homogenization probe — the pre-build GATE for #18.

RUNS ON THE HERMES HOST with the Hermes venv Python (paid — real model calls):

    /usr/local/lib/hermes-agent/venv/bin/python spikes/iterative_probe.py --out ~/.cadre/iterative-probe.md

Preview the plan + call budget with ZERO calls (works on the dev box too — no
hermes-agent needed, nothing is invoked):

    python spikes/iterative_probe.py --dry-run

--------------------------------------------------------------------------------
Why this exists
--------------------------------------------------------------------------------
Before the iterative executor is written, confirm a multi-round DEBATE produces a
SHARPER / more-differentiated synthesis than the parallel-synthesize fan-out Cadre
ALREADY ships — over the SAME models, question, and synthesizer. Two plan reviewers
independently warned debate could converge to a bland median and deliver LESS than
the fan-out at higher cost (mode collapse / sycophancy). If it homogenizes here, the
design is revisited BEFORE any executor code is written.

    PASS = the debate synthesis is sharper AND diversity survived (a contrarian /
           minority position was not steamrolled into false consensus).
    FAIL = blander than parallel, OR the contrarian collapsed into consensus, OR
           no real round-over-round movement (lanes just restate).

--------------------------------------------------------------------------------
Two arms — identical models / synthesizer / question, differing only in shape
--------------------------------------------------------------------------------
  * PARALLEL (baseline): the REAL shipped ``run_fleet`` — one fan-out round ->
    synthesize. Not a mock; the actual engine path.
  * DEBATE (test): hand-orchestrated N rounds. Round 1 is a task-only call; rounds
    2+ thread the PREVIOUS round's positions (all lanes) into each lane via the
    SHIPPED ``_thread_prompt`` (untrusted-DATA framing, previous-round-only), then
    the SAME synthesizer blends the final round. This mirrors exactly what U7's
    flagship will ship — the debate FOCUSES below are the ones it will ship, not
    hand-tuned to win this test.

Fairness controls: both arms reuse the shipped prompt builders and the SAME
``SynthesisSpec`` + synthesis prompt. The only differences are (a) rounds and (b)
each product's natural focuses. Within-round calls run serially here for a readable
transcript — each round sees only the FROZEN previous round, so the outputs are
identical to the shipped concurrent-within-round executor (concurrency changes
wall-clock, not output).

The debate transcript + both final syntheses are written to --out so the verdict
survives a context clear (the result gates the whole 9-unit build).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Run-as-script path fix (mirrors spikes/verify_aiagent_providers.py): Python puts
# spikes/ on sys.path[0], not the repo root, so the fleet_engine imports below fail
# without this. Harmless when imported.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fleet_engine.config import FleetConfig, SpecialistSpec, SynthesisSpec  # noqa: E402
from fleet_engine.engine import (  # noqa: E402
    _specialist_prompt,
    _synthesis_prompt,
    _thread_prompt,
    run_fleet,
)
from fleet_engine.model_client import AgentResult, ModelClient  # noqa: E402

# ============================================================================
# CONFIG — VERIFY these strings against the host's ~/.cadre/palette.yaml before the
# paid run (a free step). Providers are chosen for MAXIMAL diversity (three distinct
# providers), which is the whole point of the comparison.
# ============================================================================

ROUNDS = 3

# (role, provider, model) — the same three lanes power both arms. Strings VERIFIED
# against the host's ~/.cadre/palette.yaml (2026-07-01): three distinct providers
# for maximal diversity, which is the whole point of the comparison.
LANES: list[tuple[str, str, str]] = [
    ("grok", "xai-oauth", "grok-4.3"),
    ("deepseek", "openrouter", "deepseek/deepseek-v4-pro"),
    ("claude", "copilot", "claude-opus-4.8"),
]

# Same synthesizer in BOTH arms (identical -> no comparative bias). Deliberately a
# NEUTRAL model — GPT is NOT one of the three debaters, so the synthesis can't favor
# its own prior lane. Verified live in the host palette.
SYNTH_PROVIDER = "openai-codex"
SYNTH_MODEL = "gpt-5.5"

# All lanes tool-free: these questions are argumentative, so web tools would add a
# retrieval confound (different search hits) to a test about REASONING/position
# movement. [] = fail-closed zero tools (never None). Cheaper + faster too.
TOOLSET: list[str] = []

QUESTIONS: list[str] = [
    # Q1 — Cadre's own thesis (max gut-checkable for the product owner).
    "Does running a task across diverse LLM providers (e.g. Grok + DeepSeek + "
    "Claude) produce materially better output than one frontier model prompted "
    "N different ways — enough to justify the orchestration complexity? Take a "
    "clear stance.",
    # Q2 — a neutral practitioner question (independent second draw; kills n=1).
    "For domain-adapting an LLM in production in 2026, is fine-tuning or "
    "long-context + retrieval (RAG) the better default? Take a clear stance and "
    "say when the other wins.",
]

# --- Focuses -----------------------------------------------------------------
# PARALLEL: a genuinely strong (not strawman) independent-analysis baseline — each
# lane gives its best expert take and a clear stance, then the synthesizer blends.
PARALLEL_FOCUS = (
    "Analyze the question independently as a domain expert. State a clear position, "
    "give your strongest evidence-based reasoning, and name the single counterargument "
    "you find hardest to dismiss. Be specific and concrete — avoid hedging."
)

# DEBATE (the flagship focuses U7 will ship): works in round 1 (no prior data) AND
# rounds 2+ (peers' prior-round positions arrive as DATA via _thread_prompt). The
# "do not converge for the sake of agreement" line is the anti-homogenization
# control — the probe tests whether debate holds diversity EVEN with it.
DEBATE_FOCUS = (
    "Take a clear position on the question and defend it with your strongest "
    "evidence-based reasoning. When prior-round positions from the other lanes are "
    "provided as data, engage them directly: concede what is genuinely stronger, "
    "rebut what is weak, and sharpen or revise your own position accordingly. Do NOT "
    "converge for the sake of agreement — if the evidence supports a genuine "
    "disagreement or a contrarian read, preserve it and make its case. Be specific "
    "and concrete — avoid hedging."
)

SYNTH_PROMPT = (
    "Synthesize the independent expert lanes' final positions into ONE decision-useful "
    "answer to the question. Take a clear stance where the evidence supports one; "
    "preserve genuine disagreement where it does not (do NOT force a false consensus). "
    "Attribute key claims — and any position that changed — to the lane that drove it. "
    "Be specific and concrete; no hedging mush."
)


# ============================================================================
# Arms
# ============================================================================


def _synth_spec() -> SynthesisSpec:
    return SynthesisSpec(provider=SYNTH_PROVIDER, model=SYNTH_MODEL, prompt=SYNTH_PROMPT)


def _parallel_config() -> FleetConfig:
    """The shipped-engine baseline: a real parallel-synthesize FleetConfig."""
    specs = [
        SpecialistSpec(
            role=role, provider=provider, model=model, toolset=list(TOOLSET),
            focus=PARALLEL_FOCUS, effective_instruction=PARALLEL_FOCUS,
        )
        for role, provider, model in LANES
    ]
    return FleetConfig(
        name="probe-parallel",
        specialists=specs,
        synthesis=_synth_spec(),
        convergence="synthesize",
        topology="parallel",
    )


def run_parallel_arm(client: ModelClient, question: str) -> tuple[str | None, list[AgentResult]]:
    """Baseline arm — the actual shipped engine. Returns (synthesis, specialist results)."""
    result = run_fleet(_parallel_config(), question, client)
    return result.synthesis, result.specialists


def run_debate_arm(
    client: ModelClient, question: str
) -> tuple[list[list[AgentResult]], str | None]:
    """Test arm — hand-orchestrated rounds mirroring the shipped iterative design.

    Returns (transcript, synthesis) where transcript[k] is round k+1's per-lane
    results. Each round sees only the FROZEN previous round's OK outputs (threaded
    via the shipped _thread_prompt), so serial-within-round matches concurrent output.
    """
    survivors = list(LANES)                     # (role, provider, model)
    prev_round: list[tuple[str, str]] | None = None
    transcript: list[list[AgentResult]] = []

    for k in range(1, ROUNDS + 1):
        this_round: list[AgentResult] = []
        for role, provider, model in survivors:
            spec = SpecialistSpec(
                role=role, provider=provider, model=model, toolset=list(TOOLSET),
                focus=DEBATE_FOCUS, effective_instruction=DEBATE_FOCUS,
            )
            if k == 1:
                prompt = _specialist_prompt(spec, question)
            else:
                prompt, _ = _thread_prompt(spec, question, prev_round or [])
            this_round.append(
                client.run(role=role, provider=provider, model=model,
                           toolset=list(TOOLSET), prompt=prompt)
            )
        transcript.append(this_round)
        oks = [r for r in this_round if r.ok]
        if not oks:
            break                               # total collapse — stop early
        prev_round = [(r.role, r.text or "") for r in oks]
        survivors = [(r.role, r.provider, r.model) for r in oks]

    last_oks = [r for r in transcript[-1] if r.ok] if transcript else []
    synthesis: str | None = None
    if last_oks:
        prompt = _synthesis_prompt(_parallel_config(), question, last_oks)
        synth = client.run(role="synthesizer", provider=SYNTH_PROVIDER,
                           model=SYNTH_MODEL, toolset=[], prompt=prompt)
        synthesis = synth.text if synth.ok else f"[synthesizer failed: {synth.error}]"
    return transcript, synthesis


# ============================================================================
# Report
# ============================================================================


def _lane_block(r: AgentResult) -> str:
    head = f"#### {r.role} ({r.provider}/{r.model}) — {'ok' if r.ok else 'FAILED'}"
    body = r.text if r.ok else f"[{r.error}]"
    return f"{head}\n\n{body}\n"


def build_report(runs: list[dict]) -> str:
    """Assemble the durable markdown record. Arms are HONESTLY labeled here (this is
    the record + my analysis input); the blind gut-check is done at presentation."""
    out = ["# Iterative-topology homogenization probe — results\n"]
    out.append(
        f"Lanes: {', '.join(f'{r}={p}/{m}' for r, p, m in LANES)}  |  "
        f"synthesizer: {SYNTH_PROVIDER}/{SYNTH_MODEL}  |  rounds: {ROUNDS}  |  "
        f"toolset: {TOOLSET or '[] (none)'}\n"
    )
    for i, run in enumerate(runs, 1):
        out.append(f"\n---\n\n## Q{i}: {run['question']}\n")
        out.append("\n### DEBATE arm — round-by-round transcript\n")
        for k, rnd in enumerate(run["transcript"], 1):
            out.append(f"\n### Round {k}\n")
            for r in rnd:
                out.append("\n" + _lane_block(r))
        out.append("\n### DEBATE arm — final synthesis\n\n")
        out.append((run["debate_synth"] or "[no synthesis]") + "\n")
        out.append("\n### PARALLEL arm (shipped run_fleet) — final synthesis\n\n")
        out.append((run["parallel_synth"] or "[no synthesis]") + "\n")
    return "\n".join(out)


# ============================================================================
# Main
# ============================================================================


def _budget() -> tuple[int, int, int]:
    n_lanes = len(LANES)
    per_debate = n_lanes * ROUNDS + 1          # lanes x rounds + 1 synth
    per_parallel = n_lanes + 1                 # lanes + 1 synth
    total = (per_debate + per_parallel) * len(QUESTIONS)
    return per_debate, per_parallel, total


def _preview() -> str:
    per_debate, per_parallel, total = _budget()
    lines = [
        "ITERATIVE PROBE — dry run (0 calls)\n",
        f"  questions : {len(QUESTIONS)}",
        f"  lanes     : {', '.join(f'{r} ({p}/{m})' for r, p, m in LANES)}",
        f"  synth     : {SYNTH_PROVIDER}/{SYNTH_MODEL}",
        f"  rounds    : {ROUNDS}   toolset: {TOOLSET or '[] (none)'}",
        "",
        f"  per question : debate {per_debate} calls + parallel {per_parallel} calls",
        f"  TOTAL PAID CALLS : {total}",
        "",
        "  Questions:",
    ]
    for i, q in enumerate(QUESTIONS, 1):
        lines.append(f"    Q{i}. {q}")
    lines.append("\n  VERIFY lane/synth strings vs ~/.cadre/palette.yaml before the paid run.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Iterative-topology homogenization probe (#18 gate).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan + call budget and exit — zero model calls (runs on dev).")
    ap.add_argument("--out", type=str, default="",
                    help="Write the markdown report to this path (in addition to stdout).")
    args = ap.parse_args()

    if args.dry_run:
        print(_preview())
        return 0

    client = ModelClient()
    runs: list[dict] = []
    for i, question in enumerate(QUESTIONS, 1):
        print(f"[probe] Q{i}/{len(QUESTIONS)}: debate arm ({ROUNDS} rounds)...", flush=True)
        transcript, debate_synth = run_debate_arm(client, question)
        print(f"[probe] Q{i}/{len(QUESTIONS)}: parallel arm (shipped run_fleet)...", flush=True)
        parallel_synth, _ = run_parallel_arm(client, question)
        runs.append({
            "question": question,
            "transcript": transcript,
            "debate_synth": debate_synth,
            "parallel_synth": parallel_synth,
        })

    report = build_report(runs)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"\n[probe] wrote report -> {out_path}")
    else:
        print("\n" + report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
