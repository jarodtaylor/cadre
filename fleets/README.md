# Fleet examples

These are **examples**, not runnable fleets. Copy one into your host fleet
library to use it:

```bash
cp fleets/research-swarm.example.yaml ~/.cadre/fleets/research-swarm.yaml
cp fleets/code-review.example.yaml ~/.cadre/fleets/code-review.yaml
# then set provider/model strings to your host-verified values (see ~/.cadre/palette.yaml)
```

Both starter fleets are seeded into `~/.cadre/fleets/` at install (stripping
`.example` from the filename) so they are ready to configure without copying
manually.

---

## Starter fleets

### `research-swarm.example.yaml` — synthesize shape

The flagship curated fleet: a multi-provider research swarm (real-time social
via Grok, broad web via a fast model, deep analysis via a strong model) that
fans out and synthesizes **one attributed report**.

All three specialists carry explicit sourcing directives so retrieval lanes
produce grounded, linked findings rather than reciting from training memory.
The synthesizer attributes each claim to the specialist that surfaced it and
calls out conflicts.

Shape: **fan-out → synthesize**. `convergence` is absent (defaults to
`synthesize`), so every existing fleet that omits the field parses identically.

### `code-review.example.yaml` — collect shape

A four-model code review swarm: security, architecture, performance, and
correctness lenses run in **parallel**, each on a different provider/model for
independent perspective. No synthesizer runs — the fleet returns **attributed
specialist outputs** on stdout for the caller to review.

Pass the code or diff you want reviewed as the `--task` argument. All four
lanes use `toolset: []` (fail-closed zero tools) because code reviewers reason
over the provided context, not live retrieval.

Shape: **fan-out → collect**. `convergence: collect` is explicit in the spec.

---

## The two fleet shapes

| Shape | `convergence:` | Output |
|---|---|---|
| `synthesize` | `synthesize` (default, may be omitted) | One synthesized report on stdout |
| `collect` | `collect` (must be explicit) | Attributed specialist blocks on stdout |

**Synthesize** is the default: the synthesizer model reads all specialist
outputs and produces a single, attributed consensus report. Best for research
and summarization tasks where you want one integrated answer.

**Collect** skips the synthesizer: each specialist's raw output is returned
with its role and model labeled. Best for review tasks (code, document, plan)
where you want independent perspectives without a model collapsing them into
one voice.

---

## `palette.example.yaml` — candidate-seed / palette template

The install seeds `~/.cadre/palette-candidates.yaml` from this file. Edit it
to match the providers you've authenticated in your Hermes profile, then run
the verify step:

```bash
PYBIN="${CADRE_HERMES_PYTHON:-$(grep -E '^CADRE_HERMES_PYTHON=' ~/.cadre/config | cut -d= -f2-)}"
"$PYBIN" spikes/verify_aiagent_providers.py
```

Verification keeps only the `(provider, model)` pairs that actually resolve on
your host and writes the confirmed `~/.cadre/palette.yaml` — the menu an agent
uses when composing new fleets. See `docs/RUNBOOK.md` for the full install and
verification flow.

---

Never commit API keys or tokens — credentials live in Hermes auth/env.
