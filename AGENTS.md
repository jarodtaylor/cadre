# AGENTS.md — guide for AI agents working in this repo

Orientation for any AI agent (Claude, Codex, or other) reading, reviewing, or
editing this repository. Product north star: `STRATEGY.md`. Human overview: `README.md`.

**What this is:** a provider‑neutral Python engine that runs ephemeral, multi‑model
agent *fleets* — fan a task out across specialists (different models/providers),
then synthesize one grounded, attributed report. All model calls go through one
thin adapter over Hermes's `AIAgent` library.

## ⚠️ Verify Hermes/AIAgent behavior — do not guess

The **only** external integration is Hermes's `AIAgent` (NousResearch/hermes-agent),
and it is **pre‑1.0 and volatile** (calendar‑versioned). Before asserting *anything*
about its constructor params, toolset names, timeouts, or provider/model resolution
— in code, comments, **or a code review** — consult the authoritative sources, not
training data:

- **`docs/reference/hermes/README.md`** — provenance + **verified integration facts** (start here).
- **`docs/reference/hermes/python-library.md`** — vendored `AIAgent` library guide (offline).
- **`docs/reference/hermes/llms.txt`** — vendored docs index (titles + canonical URLs).
- Live, when web is available: `https://hermes-agent.nousresearch.com/docs/llms-full.txt` (full docs) and <https://github.com/NousResearch/hermes-agent>.

Guessing here already caused a real privilege bug (`enabled_toolsets=None` enables
**every** toolset incl. shell/file/browser; `[]` enables none). When unsure, cite the
source file/line or the vendored doc — or say it's unverified. Highest‑leverage facts
are in the reference README; skim it before reviewing the adapter, the config gate, or the engine timeout.

**Do not live-import `hermes-agent` / `run_agent`** to introspect the real API — it
is not installed in dev or review sandboxes (the import fails), and installing it
there is the wrong fix (it is host-only and needs auth). Read the vendored
`python-library.md` instead; that is exactly what it is for.

## Dev vs runtime split

- **This repo runs and tests fully WITHOUT `hermes-agent`.** The adapter
  (`fleet_engine/model_client.py`) lazy‑imports `run_agent` only when constructing a
  live agent; every test uses a fake/stub. So the engine imports and the suite passes
  on a plain machine.
- **Dev machine:** use the project venv — `.venv` (Python **3.11**). System Python may
  be too new for `hermes-agent` (which requires `<3.14`). Dev deps are just `pyyaml`.
- **Hermes host (VPS / Mac Mini):** the only place fleets run live — `hermes-agent`
  installed, providers authenticated. Deploy/dogfood steps: `docs/RUNBOOK.md`.

## How to run

```bash
.venv/bin/python -m unittest discover -s tests                       # full suite (stdlib unittest)
.venv/bin/python -m fleet_engine.cli validate fleets/research-swarm.example.yaml
.venv/bin/python -m fleet_engine.cli run <spec.yaml> --task "…"       # needs hermes-agent (host)
```

## Conventions

- **Tests:** stdlib `unittest` (no pytest). Fixtures: `make_data(**overrides)` /
  fake‑factory or fake‑client injection; **no live model calls in the suite.** Tests
  green before every commit.
- **Git:** feature branches only, never `main`; conventional commits.
- **Architecture seams (keep them):**
  - `model_client.py` is the **only** module aware of `AIAgent` (the isolation seam,
    lazy import). The engine holds **no** `AIAgent` knowledge and **no** fleet‑domain strings.
  - The config toolset gate is a **fail‑closed allowlist** (`SAFE_TOOLSETS` in
    `fleet_engine/config.py`); anything not safe needs `allow_privileged_tools: true`.
    Do not regress it to a denylist.
  - The engine bounds every model call with a daemon‑thread wall‑clock backstop
    (`call_timeout`); see the module docstring for why it's daemon threads, not
    `ThreadPoolExecutor`, and why it's a backstop over AIAgent's own request timeout.
- **Schema:** minimal — one primitive (fan‑out → synthesize). Don't add abstraction
  for hypothetical second primitives.

## Repo layout for reviewers

**Committed (the product):** `fleet_engine/`, `tests/`, `fleets/`, `skills/`,
`spikes/`, `docs/reference/`, `docs/RUNBOOK.md`, `STRATEGY.md`, `README.md`,
`requirements*.txt`, this file.

**Gitignored (local process, NOT in the tree you review):** `docs/brainstorms/`,
`docs/plans/`, `docs/solutions/`, `docs/specs/`, `CLAUDE.md`, `.claude/`, `.venv/`.
Don't expect planning/design docs in a review — the committed tree *is* the tool.
