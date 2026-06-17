---
name: research-swarm
description: Run a multi-provider research swarm — fan out across models in parallel, then synthesize one grounded, attributed report.
version: 0.1.0
metadata:
  hermes:
    category: research
    tags: [multi-agent, multi-model, research, synthesis]
    requires_toolsets: [terminal]
---

# Research Swarm

Fan out a research task across several model providers in parallel (each a
specialist with its own model and toolset), then synthesize one grounded report
that attributes each claim to the specialist/model that surfaced it.

> Note: this is a thin wrapper over the standalone engine. Verify the frontmatter
> above against your Hermes version when installing — the engine and `run.py` are
> what do the work.

## When to use

When a research question benefits from multiple providers at once — e.g.
real-time social via Grok, cheap broad web sweeps via a fast model, deeper
analysis via a strong model — and you want a single synthesized, citeable result.

## Prerequisites

- This repo deployed on the Hermes host, with `hermes-agent` installed in the
  Hermes venv (pin it in `requirements.txt`) and the fleet's providers
  authenticated.
- A real `fleets/research-swarm.yaml` — copy `fleets/research-swarm.example.yaml`
  and set your confirmed provider + model strings (the U1 spike confirms them).
- Search-tool credentials for the lanes used (web: Tavily/Serper/Brave;
  x_search: SuperGrok OAuth or an xAI key).

## Procedure

1. Gather the research task / query from the user.
2. Run the fleet with the Hermes venv Python:
   ```bash
   ~/.hermes/hermes-agent/venv/bin/python "${HERMES_SKILL_DIR}/run.py" --task "$TASK"
   ```
3. Return the synthesized report. It ends with a provenance section showing which
   specialist/model produced what, plus any failure or degradation notes.
