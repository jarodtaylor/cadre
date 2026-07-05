# Cadre

Provider-neutral, ephemeral, **multi-model agent fleets**. Fan a task out across whatever models you have — Grok, Gemini, Claude, GPT, OpenRouter, or local — each a specialist with its own model and toolset, then **synthesize** them into one grounded, attributed result, **collect** their independent findings side by side, or have an independent model **judge** each one. Run the lanes in parallel, or as a **sequential chain** where each stage audits the last. Built on [Hermes](https://hermes-agent.nousresearch.com)'s `AIAgent` library.

> **Status: building in public.** Shipped and **dogfooded live** on a Hermes host: the engine, run-capture, live per-lane progress, the agent-run handoff, cross-model **review fleets** (code-review + doc-review), a **sequential research-brief chain**, and **iterative debate and critique-revise fleets** — across `synthesize`, `collect`, and `judge` convergence and **parallel**, **sequential**, and **iterative** topology. A Hermes agent has driven a four-model fleet end-to-end (preview → human okay → grounded, attributed result). Early and evolving — expect APIs to change.

## Why

**Where it came from.** I build Hermes agents. When one needs to spread work across subagents — review its own plan, validate a diff, research something — Hermes's `delegate_task` can't vary the model: every subagent runs the one default (or a single configured delegate model for *all* of them). Cadre fixes that. Spin up an ephemeral fleet where each specialist is its own provider and model — no full Hermes profile, workspace, or memory required, just a YAML fleet.

The primitive is general: **any fleet you can describe in config**, not a fixed menu. We pre-bake a few to get you started — a research swarm, a code-review swarm, and a docs-review swarm — and the aim is harness/runtime-agnostic, so you run your fleets wherever your agents live (Hermes today; more runtimes as reach).

A single-vendor runtime can only fan a task out across one provider's own tiers. Cadre routes the right subtask to the right *provider* — real-time social from Grok, broad web from a fast model, deep analysis from a strong one, a contrarian read from another — then synthesizes one report that **attributes each claim to the model that surfaced it**. Decomposing a task into specialist lenses is what drives coverage; running those lenses across *diverse* models adds independent cross-checking and resilience, and surfaces what any single model's blind spots miss. See [STRATEGY.md](STRATEGY.md).

## How it works

A *fleet* is defined entirely in YAML — specialists (each a role + provider + model + toolset) plus an optional synthesizer — and has two independent **shape axes**:

- **Topology** — how lanes relate in time: **parallel** (independent and concurrent, the default fan-out), **sequential** (a dependent chain where each stage consumes all preceding stages' output — e.g. a scout gathers sources, then an analyst *audits the scout's specific claims* against live verification, then a writer synthesizes the audited brief), or **iterative** (lanes run for multiple rounds; from round 2 each lane sees the prior round's attributed outputs from all other lanes — enabling debate, critique-revise loops, and self-refinement; the round-by-round transcript is preserved in the run folder).
- **Convergence** — what happens to the outputs: **synthesize** (a strong model combines the survivors into one grounded report), **collect** (no synthesizer — return each specialist's attributed output side by side, as the review fleets do), or **judge** (an independent critic grades each surviving specialist in place, attributed per lane).

Synthesize degrades rather than crashing: if some lanes fail it synthesizes the rest and reports the failures, failing outright only when none survive. A sequential chain breaks on the first failed stage and marks the rest skipped.

```mermaid
flowchart TB
    T["task"] --> E["engine"]
    E -->|"fan out (parallel)"| S1["specialist A<br/>provider / model / toolset"]
    E -->|"fan out"| S2["specialist B"]
    E -->|"fan out"| S3["specialist C"]
    S1 --> G["gather survivors"]
    S2 --> G
    S3 --> G
    G --> SY["synthesizer<br/>(strong model)"]
    SY --> R["grounded, attributed report<br/>+ per-lane provenance"]
```

*…or the same fleet model runs as a **sequential chain** — each stage consumes all previous stages' output (the `research-brief` flagship: scout gathers → analyst audits the scout's specific claims → writer synthesizes):*

```mermaid
flowchart LR
    T2["task"] --> SC["scout<br/>web + search"]
    SC -->|"output threaded as context"| AN["analyst<br/>audits scout's claims"]
    AN -->|"+ all prior context"| WR["writer<br/>no tools"]
    WR --> R2["grounded brief<br/>attributed, chain-audited"]
```

All model calls are isolated behind one thin adapter over Hermes's `AIAgent` (`cadre/model_client.py`), so the engine **runs and tests without Hermes installed** — the rest of the suite uses fakes. Each model call is bounded by a wall-clock backstop, and the toolset gate is a **fail-closed allowlist**: a specialist (which may read untrusted web content) only gets read/search/analyze tools unless a fleet explicitly opts into `allow_privileged_tools: true`.

Every run is **captured** to `~/.cadre/runs/<timestamp-slug>/` — per-specialist markdown, the synthesis, and a JSON manifest (per-lane outcome, elapsed, toolset, timed-out; run-level synth status + active profile) — so a run is auditable after the fact.

## Using it with a Hermes agent (the agent-run handoff)

A Hermes agent can run Cadre conversationally through the discoverable **`cadre-fleet` skill**: pick a curated fleet or compose one from a host-verified palette, **preview the actual parsed fleet for a human okay**, run it, and weave back the grounded, attributed result.

Two files do two different jobs:

| File | Role | Who writes it |
|---|---|---|
| `~/.cadre/palette.yaml` | The **menu** — `(provider, model)` pairs verified to resolve on *this* host, plus the safe toolsets the profile declares | Generated by install from `palette-candidates.yaml` you edit |
| `~/.cadre/fleets/<name>.yaml` | The **recipe** — which specialists (role + model + toolset) and synthesizer make up a fleet, composed from the menu | You or the agent |

```mermaid
flowchart LR
    subgraph Install["one-time install (per host)"]
        C["palette-candidates.yaml<br/>you edit — the menu"] -->|"verify live"| P["palette.yaml<br/>verified models + safe tools"]
    end
    subgraph Runtime["a Hermes agent, per task"]
        F["~/.cadre/fleets/&lt;name&gt;.yaml<br/>the fleet"] --> V["run.py --preview"]
        V --> H{"human okays?"}
        H -->|"yes"| RUN["run.py --task ..."]
        RUN --> ENG["engine: fan-out → synthesize"]
        ENG --> W["weave back<br/>attributed + run folder"]
    end
    P -. "compose from" .-> F
```

The **preview is the operative control**: it renders mechanically from the parsed `FleetConfig` (the synthesizer, `allow_privileged_tools`, the synthesis prompt, and every lane) and exits without making a model call — so a human approves *what actually runs*, not the agent's paraphrase. Safe toolsets still read untrusted content and the synthesis is consumed by a terminal-capable agent, so prompt-injection/SSRF is a named, deferred risk; see `cadre/data/skill/SKILL.md` and `docs/RUNBOOK.md`.

## Quick start (development)

```bash
python3.11 -m venv .venv          # Python >=3.11,<3.14
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests                                   # run the suite
.venv/bin/python -m cadre.cli validate cadre/data/fleets/research-swarm.example.yaml
```

Repo-present, these run with no install — the top-level `cadre` package imports
from the working directory. The engine imports and the suite passes **without**
`hermes-agent` (the adapter lazy-imports it; tests use fakes).

## Installing `cadre`

```bash
pip install "git+https://github.com/jarodtaylor/cadre@<ref>"   # pin a tag or commit
```

A bare `pip install cadre` grabs a different, unrelated package on PyPI — always
install from the git URL above until `cadre` publishes under its own name. On a
Hermes host, install into the **Hermes venv Python** (the interpreter that runs
`run_agent`) — see `docs/RUNBOOK.md` for the full install → provision → verify →
run checklist, including the `cadre setup` / `cadre verify-palette` / `cadre
install-skill` verbs and the agent-run handoff's `cadre/data/skill/`.

## Running live

Running a fleet for real needs a Hermes host with `hermes-agent` installed and providers authenticated. `docs/RUNBOOK.md` is the ordered checklist: install → `cadre setup` (provisions `~/.cadre`, auto-seeding all seven starter fleets from the installed package) → `cadre verify-palette` → edit a seeded fleet's provider/model strings → preview → run. Never commit API keys or tokens; credentials live in Hermes auth/env.

## Roadmap

v0 — the engine, run-capture, and the Hermes **agent-run handoff** — is shipped and dogfooded live. v1 is organized into four tracks (filter the issues by their `track:` label, or see the [**v1 milestone**](https://github.com/jarodtaylor/cadre/milestone/1)):

- **Operator & agent experience** (`track: operator-dx`) — runs that feel alive and trustworthy.
- **Fleet library & primitives** (`track: fleet-library`) — batteries-included fleets + new primitives.
- **Trust & safety hardening** (`track: trust-safety`) — the human-approval gate, bulletproof.
- **Reach & packaging** (`track: reach`) — a real `cadre` package + more runtime wrappers.

See [STRATEGY.md](STRATEGY.md) for the full direction.

## Learn more

- **[CONCEPTS.md](CONCEPTS.md)** — shared vocabulary (fleet, specialist, synthesizer, verified palette, fleet library, fleet preview, and the topology × convergence shape model).
- **[SECURITY.md](SECURITY.md)** — what the defensive hardening protects against (display spoofing, forged judge markers, install seeding) and what stays a bounded residual (semantic prompt injection). Cadre is not "injection-proof" — this says plainly what is and isn't defended.
- **[STRATEGY.md](STRATEGY.md)** — the product's target problem, approach, and tracks of work.
- **[AGENTS.md](AGENTS.md)** — orientation for AI agents working in this repo.
- **`docs/RUNBOOK.md`** — deploy, install, and the agent-run usage loop.

## License

[MIT](LICENSE) © 2026 Jarod Taylor
