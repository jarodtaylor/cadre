# Agent Fleet Factory

Provider-neutral, ephemeral, **multi-model agent fleets**. Fan a task out across whatever models you have — Grok, Gemini, Claude, GPT, OpenRouter, or local — each as a specialist, then synthesize one grounded, attributed report. Built on [Hermes](https://hermes-agent.nousresearch.com)'s `AIAgent` library.

> **Status: early WIP, building in public.** The engine core is built and unit-tested; the live multi-provider demo runs on a configured Hermes host and is in progress. Expect APIs to change.

## Why

Single-vendor runtimes fan out only within one provider's tiers. This routes the right subtask to the right *provider* — then synthesizes. See [STRATEGY.md](STRATEGY.md).

## How it works

A small engine runs one orchestration primitive — **parallel fan-out → synthesize** — over a fleet defined entirely in YAML (specialists with their provider/model/toolset, plus a synthesis step). All model calls are isolated behind a thin adapter over Hermes's `AIAgent`, so the engine runs and tests without Hermes installed.

```
   config (YAML)
        │
        ▼
   engine  ──fan out──▶  specialists (one model each, in parallel)
        │                      │
        │◀─────gather──────────┘
        ▼
   synthesize (strong model) ──▶  grounded, attributed result
        ▲
   adapter ──▶ Hermes AIAgent          entry surfaces: CLI + Hermes skill
```

## Quick start (development)

```bash
python3.11 -m venv .venv          # Python >=3.11,<3.14
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests      # run the suite
.venv/bin/python -m fleet_engine.cli validate fleets/research-swarm.example.yaml
```

Running a fleet *live* needs a Hermes host with `hermes-agent` installed and providers authenticated — see `skills/research-swarm/SKILL.md`. Copy `fleets/research-swarm.example.yaml` to `fleets/research-swarm.yaml` and set your real provider + model strings. Never commit API keys or tokens.

## License

TBD
