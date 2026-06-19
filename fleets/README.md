# Fleet examples

These are **examples**, not runnable fleets. Copy one into your host fleet
library to use it:

```bash
cp fleets/research-swarm.example.yaml ~/.cadre/fleets/research-swarm.yaml
# then set provider/model strings to your host-verified values (see ~/.cadre/palette.yaml)
```

- **`research-swarm.example.yaml`** — the flagship curated fleet: a multi-provider
  research swarm (real-time social via Grok, broad web via a fast model, deep
  analysis via a strong model) that fans out and synthesizes one attributed report.
  Replace the provider/model strings with the exact ones your Hermes resolves.
- **`palette.example.yaml`** — the candidate-seed / palette template. The install
  seeds `~/.cadre/palette-candidates.yaml` from it; the verify step then writes the
  host-confirmed `~/.cadre/palette.yaml` (the menu an agent composes new fleets from).

The repo `fleets/` directory is examples-only; the **runnable** library lives at
`~/.cadre/fleets/` (owner-only, created at install). See `docs/RUNBOOK.md` for
install + the agent-run handoff, and `skills/cadre-fleet/SKILL.md` for how an
agent selects, previews, and runs a fleet.

Never commit API keys or tokens — credentials live in Hermes auth/env.
