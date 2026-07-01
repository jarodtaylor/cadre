# AGENTS.md — guide for AI agents working in this repo

Orientation for any AI agent (Claude, Codex, or other) reading, reviewing, or
editing this repository. Product north star: `STRATEGY.md`. Human overview: `README.md`.

**What this is:** a provider‑neutral Python engine that runs ephemeral, multi‑model
agent *fleets* — fan a task out across specialists (different models/providers),
then **synthesize** one grounded, attributed report (the default), **collect** the
raw attributed outputs for the caller to review, or **judge** them with an
independent critic that grades each specialist's output in place (attributed, not
blended). All model calls go through one thin adapter over Hermes's `AIAgent` library.

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
.venv/bin/python skills/cadre-fleet/run.py --fleet fleets/research-swarm.example.yaml --preview  # render parsed fleet, no model calls (dev-safe)
.venv/bin/python -m fleet_engine.cli run <spec.yaml> --task "…"       # needs hermes-agent (host)
.venv/bin/python -m fleet_engine.cli run <spec.yaml> --doc plan.md --task "Review this PLAN"  # --doc reads a file into the task (repeatable; either --task or --doc)
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
  - **Agent-run handoff:** `skills/cadre-fleet/` is the runtime surface a Hermes agent
    invokes (select a curated fleet from `~/.cadre/fleets/`, or compose one only from
    the verified `~/.cadre/palette.yaml`). The runner's `--preview` (`render_fleet_preview`)
    renders the **parsed** `FleetConfig` and is the load-bearing human-okay control — the
    human approves what runs, not the agent's paraphrase. **Do not relax preview-always**
    without the deferred security pass: safe toolsets still read untrusted web content,
    and the synthesis is consumed by a terminal-capable agent, so an SSRF/injection in a
    lane reaches beyond a tainted report. Owner-only `~/.cadre` perms guard other OS users,
    not the agent itself.
- **Schema:** minimal. Two execution topologies via an explicit `topology:
  parallel|sequential` field — **parallel** (default; independent concurrent fan‑out)
  or **sequential** (a dependent chain where each lane consumes all preceding successful
  lanes' output, accumulated through the chain) — and an explicit `convergence:
  synthesize|collect|judge` field — **synthesize** (default; a strong model blends the
  survivors), **collect** (no synthesizer; return the raw attributed outputs), or **judge**
  (an independent critic grades each survivor in place; requires a `judge:` block with
  provider/model/prompt). Both `topology` and `convergence` default to their original
  values, so every pre‑existing fleet parses unchanged. Don't add abstraction for the
  *deferred* topology axis (iterative — see `CONCEPTS.md`) until it lands.
- **Convergence‑aware consumers (explicit `status` — keep it):** `FleetResult` carries an
  explicit `FleetStatus` tri-state (`SUCCESS` / `DEGRADED` / `FAILED`) set at every
  `run_fleet` return point. Consumers read `result.status` for run outcome — **do not
  re-derive it** from `ok`, `synth_ok is None`, or `synthesis is None`. Derived reads:
  `ok` (`status is SUCCESS`) is the exit-code signal; `has_usable_output()` (`status is
  not FAILED`) covers SUCCESS and DEGRADED. `synth_ok`/`judge_ok` remain as mode-specific
  detail; `manifest.json` also carries a top-level `"status"` string (serialized as
  `success`/`degraded`/`failed`) alongside them.
  Every consumer — process exit code, `render` header, `capture` manifest, `cli` — still
  reads `result.convergence` to understand what the result *contains* (synthesize →
  `synthesis` populated; collect → attributed specialist blocks, no synthesis; judge →
  `judge` raw text + per-lane `judge_ok`). Do not add a consumer that infers mode or
  outcome from `synthesis is None` / `synth_ok is None` alone — always read `convergence`
  for shape, `status` for outcome. Consumers also read `result.topology` for execution
  shape (parallel vs sequential chain) the same way they read `result.convergence` for
  output shape. The sequential chain executor owns a **completion-based** status
  independent of collect’s unconditional SUCCESS: a broken `sequential + collect` chain
  (at least one lane completed, a later lane failed) is DEGRADED, not SUCCESS;
  `sequential + synthesize`/`judge` is conjunctive — SUCCESS only if the chain completed
  AND the convergence step succeeded.
- **Sequential/chain seams:** `_run_chain` is engine-internal and stays pure (events
  only — it emits `LaneStarted`/`LaneDone`, never I/O); it threads each prior successful
  lane’s attributed output into the next lane’s prompt, capped **per stage**
  (`_CHAIN_DELIM` / `_CHAIN_STAGE_CAP`) so no single stage is dropped; it breaks on the
  first failure, marking downstream lanes **skipped** (excluded from `failures`, the
  failure notes, and the failure tally — recorded as a distinct manifest state); it sets
  `terminal_produced` (a deliverable present vs scaffolding only) and
  `threading_truncated`; each lane gets a freshly-recomputed **per-lane** deadline (not a
  shared budget — a slow lane 1 would otherwise starve lane 2 into a false timeout); and
  it reuses `_run_convergence` for the synth/judge output step. **Cross-lane trust edge:**
  each lane’s untrusted output becomes the next lane’s prompt context — a stronger
  injection vector than parallel fan-out, because an injection in lane N can steer lane
  N+1’s tool use directly. The per-lane `SAFE_TOOLSETS` allowlist is the first control;
  forgery-hardening the threading delimiter is GH #5’s scope, and #5 must account for
  this sequential propagation path, not just the parallel posture.
- **Judge‑specific seams:** the engine returns the judge's raw text in `result.judge`
  and stays pure (no parsing). `fleet_engine/judge_grade.py` (caller‑layer) parses it
  into per-lane structure (`{role, model, grade, rationale}`, plus `ungraded` lanes and
  `parsed_ok`). **Prompt↔parser label contract:** `_judge_prompt` labels each surviving
  specialist by its exact `role` string (response format: `=== LANE: <role> ===` /
  `Grade:` / `Rationale:`); `parse_grades` matches each grade entry to a surviving lane
  on that exact key. Label drift (paraphrase or wrong role name) degrades toward
  *false-partial* (lane flagged ungraded) — never *false-full* (skipped lane hidden).
  Partial coverage exits 0; only a judge error/timeout or total specialist failure exits
  1. The judge passes `toolset=[]` explicitly — the `[]`-vs-`None` invariant applies
  (never `None`, which enables every tool over untrusted specialist text).
- **Preview/validate is a trust surface:** `fleet_engine/preview_lint.py` (palette +
  focus‑grounding validation) is caller‑layer — imported only by `run.py`/`cli.py`,
  never by the engine — and warns, never blocks. Every fleet‑controlled string printed
  by any preview/validate surface (the rendered fleet, the lint warnings, the validate
  summary) goes through `render._sanitize`: the preview is the human‑okay control and
  must not be spoofable by terminal escapes in a tampered fleet.
- **Caller‑side file input (`--doc`):** `fleet_engine/file_input.py` is caller‑layer —
  imported only by `run.py`/`cli.py`, never by the engine (`TestEngineIsolation` guards
  both directions). `compose(task, docs)` reads each `--doc PATH` and appends it to the
  task as a labeled `=== FILE: <path> ===` block; the engine still receives only the
  finished task **string** and gains no file I/O. An unreadable/missing/non‑UTF‑8 `--doc`
  raises `ConfigError` (named path), caught by the same handler that guards `load` +
  `resolve`; oversize files truncate at `MAX_FILE_BYTES` with a visible note. `--preview`
  lists the `--doc` paths as given — not canonicalized (`render.render_file_inputs`,
  sanitized) — and read‑checks them.
  **Boundary:** the path *labels* shown in the preview are `_sanitize`d, but the injected
  file *content* is **not** — sanitizing it would corrupt the reviewed document;
  output‑side content hardening is the deferred #5/#23 surface.

## Repo layout for reviewers

**Committed (the product):** `fleet_engine/`, `tests/`, `fleets/`, `skills/`,
`spikes/`, `docs/reference/`, `docs/RUNBOOK.md`, `STRATEGY.md`, `README.md`,
`requirements*.txt`, this file. Also committed, as contributor knowledge:
**`docs/solutions/`** — documented solutions to past problems (bugs, patterns,
conventions), by category with YAML frontmatter (`module`, `tags`, `problem_type`),
relevant when implementing or debugging in a documented area; and **`CONCEPTS.md`** —
the shared domain vocabulary, relevant when orienting to the code.

**Gitignored (internal process, NOT in the tree you review):** `docs/brainstorms/`,
`docs/plans/`, `docs/specs/`, `CLAUDE.md`, `.claude/`, `.codex/`, `.venv/`.
Don't expect planning/design docs in a review — the committed tree *is* the tool.
