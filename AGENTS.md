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
  (`cadre/model_client.py`) lazy‑imports `run_agent` only when constructing a
  live agent; every test uses a fake/stub. So the engine imports and the suite passes
  on a plain machine.
- **Dev machine:** use the project venv — `.venv` (Python **3.11**). System Python may
  be too new for `hermes-agent` (which requires `<3.14`). Dev deps are just `pyyaml`.
- **Hermes host (VPS / Mac Mini):** the only place fleets run live — `hermes-agent`
  installed, providers authenticated. Deploy/dogfood steps: `docs/RUNBOOK.md`.

## How to run

```bash
.venv/bin/python -m unittest discover -s tests                       # full suite (stdlib unittest)
.venv/bin/python -m cadre.cli validate cadre/data/fleets/research-swarm.example.yaml
.venv/bin/python -m cadre.cli run <spec.yaml> --task "…"       # needs hermes-agent (host)
.venv/bin/python -m cadre.cli run <spec.yaml> --doc plan.md --task "Review this PLAN"  # --doc reads a file into the task (repeatable; either --task or --doc)
```

Repo-present, these run with no install (the top-level `cadre` package imports from
the working directory — R9). The installed `cadre` command (`pip install .` or the
git URL — see `docs/RUNBOOK.md`) exposes the same `validate`/`run` behavior as a
console script, plus `setup`/`verify-palette`/`install-skill` for host provisioning.

## Conventions

- **Tests:** stdlib `unittest` (no pytest). Fixtures: `make_data(**overrides)` /
  fake‑factory or fake‑client injection; **no live model calls in the suite.** Tests
  green before every commit.
- **Git:** feature branches only, never `main`; conventional commits.
- **Architecture seams (keep them):**
  - `model_client.py` is the **only** module aware of `AIAgent` (the isolation seam,
    lazy import). The engine holds **no** `AIAgent` knowledge and **no** fleet‑domain strings.
  - The config toolset gate is a **fail‑closed allowlist** (`SAFE_TOOLSETS` in
    `cadre/config.py`); anything not safe needs `allow_privileged_tools: true`.
    Do not regress it to a denylist.
  - The engine bounds every model call with a daemon‑thread wall‑clock backstop
    (`call_timeout`); see the module docstring for why it's daemon threads, not
    `ThreadPoolExecutor`, and why it's a backstop over AIAgent's own request timeout.
  - **Agent-run handoff:** the `cadre-fleet` skill (source `cadre/data/skill/`,
    materialized into a Hermes skills directory by `cadre install-skill`) is the
    runtime surface a Hermes agent invokes (select a curated fleet from
    `~/.cadre/fleets/`, or compose one only from the verified `~/.cadre/palette.yaml`).
    The runner's `--preview` (`render_fleet_preview`)
    renders the **parsed** `FleetConfig` and is the load-bearing human-okay control — the
    human approves what runs, not the agent's paraphrase. **Do not relax preview-always**
    without the deferred security pass: safe toolsets still read untrusted web content,
    and the synthesis is consumed by a terminal-capable agent, so an SSRF/injection in a
    lane reaches beyond a tainted report. Owner-only `~/.cadre` perms guard other OS users,
    not the agent itself.
- **Schema:** minimal. Three execution topologies via an explicit `topology:
  parallel|sequential|iterative` field — **parallel** (default; independent concurrent
  fan‑out), **sequential** (a dependent chain where each lane consumes all preceding
  successful lanes' output, accumulated through the chain), or **iterative** (lanes run
  in rounds; within a round all lanes run concurrently; from round 2 each lane sees the
  previous round's attributed outputs from all lanes as untrusted data; a failed lane is
  dropped; survivors carry into the next round; `rounds` sets the count, 1–10) — and an
  explicit `convergence: synthesize|collect|judge` field — **synthesize** (default; a
  strong model blends the survivors), **collect** (no synthesizer; return the raw
  attributed outputs), or **judge** (an independent critic grades each survivor in place;
  requires a `judge:` block with provider/model/prompt). All `topology` and `convergence`
  values default to their original values, so every pre‑existing fleet parses unchanged.
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
  shape (parallel vs sequential chain vs iterative rounds) the same way they read
  `result.convergence` for output shape. The sequential chain executor owns a
  **completion-based** status independent of collect’s unconditional SUCCESS: a broken
  `sequential + collect` chain (at least one lane completed, a later lane failed) is
  DEGRADED, not SUCCESS; `sequential + synthesize`/`judge` is conjunctive — SUCCESS only
  if the chain completed AND the convergence step succeeded. Under iterative topology,
  consumers additionally read `result.diversity_collapsed` (bool) — set when the last
  surviving round has ≤1 ok lane or zero cross-round iterations occurred; advisory, never
  changes `status`.
- **Sequential/chain seams:** `_run_chain` is engine-internal and stays pure (events
  only — it emits `LaneStarted`/`LaneDone`, never I/O); it threads each prior successful
  lane’s attributed output into the next lane’s prompt, capped **per stage**
  (`_CHAIN_DELIM` / `CHAIN_STAGE_TOKEN_CAP`) so no single stage is dropped; it breaks on the
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
- **Iterative seams:** `_run_iterate` is engine-internal and stays pure (events only —
  no I/O). Within each round, all lanes run concurrently (same concurrency model as
  parallel fan-out). After each round, every surviving lane’s attributed output is
  threaded into the next round’s prompt for **all** lanes as untrusted data. This creates
  a **cross-round injection surface**: a prompt-injection in one lane’s round-N output
  enters every other lane’s round-N+1 context, compounding over rounds — a broader
  exposure than sequential (one-directional downstream) and than parallel (no cross-lane
  sharing at all). The per-lane `SAFE_TOOLSETS` allowlist is the first control; GH #5
  must account for this iterative cross-round propagation path, not just the parallel and
  sequential postures. `--preview` discloses cross-lane tool exposure when a lane with
  retrieval tools has its output threaded into other lanes’ contexts. `diversity_collapsed`
  is set by the engine at the `run_fleet` return point and surfaced in render + manifest;
  it never affects `status`.
- **Judge‑specific seams:** the engine returns the judge's raw text in `result.judge`
  and stays pure (no parsing). `cadre/judge_grade.py` (caller‑layer) parses it
  into per-lane structure (`{role, model, grade, rationale}`, plus `ungraded` lanes and
  `parsed_ok`). **Prompt↔parser label contract (nonce-bound, #5):** `_judge_prompt`
  labels each surviving specialist by its exact `role` string plus a per-run marker
  nonce (`result.judge_marker_nonce`, `secrets.token_hex`) — response format
  `=== LANE: <role> <nonce> ===` / `Grade:` / `Rationale:`; `parse_grades(…, marker_nonce)`
  requires the nonce, so a nonce-free `=== LANE:` (a specialist can't know the per-run
  nonce, never seeing the judge prompt) is ignored. Emitter and parser are a cross-module
  format contract bound by a coupling test. Label drift (paraphrase or wrong role name)
  degrades toward *false-partial* (lane flagged ungraded) — never *false-full* (skipped lane hidden).
  Partial coverage exits 0; only a judge error/timeout or total specialist failure exits
  1. The judge passes `toolset=[]` explicitly — the `[]`-vs-`None` invariant applies
  (never `None`, which enables every tool over untrusted specialist text).
- **Preview/validate is a trust surface:** `cadre/preview_lint.py` (palette +
  focus‑grounding validation) is caller‑layer — imported only by `run.py`/`cli.py`,
  never by the engine — and warns, never blocks (the separate caller‑layer
  `cadre/preflight.py` gate refuses an off‑palette *model* before spend — #62 — but
  `preview_lint` itself only warns). Every fleet‑controlled string printed
  by any preview/validate surface (the rendered fleet, the lint warnings, the validate
  summary) goes through `cadre.text_safety.sanitize` (the public chokepoint since
  #5/#23; `render._sanitize` is now a compat alias): the preview is the human‑okay control
  and must not be spoofable by terminal escapes in a tampered fleet. **Since #5 the same
  `sanitize` also covers model *output* — specialist text, synthesis/judge bodies, error
  strings, and model-derived `manifest.json` fields — on both the terminal and on-disk
  capture surfaces; only `--doc`/prompt *input* content stays raw (below).**
- **Caller‑side file input (`--doc`):** `cadre/file_input.py` is caller‑layer —
  imported only by `run.py`/`cli.py`, never by the engine (`TestEngineIsolation` guards
  both directions). `compose(task, docs)` reads each `--doc PATH` and appends it to the
  task as a labeled `=== FILE: <path> ===` block; the engine still receives only the
  finished task **string** and gains no file I/O. An unreadable/missing/non‑UTF‑8 `--doc`
  raises `ConfigError` (named path), caught by the same handler that guards `load` +
  `resolve`; oversize files truncate at `MAX_FILE_BYTES` with a visible note. `--preview`
  lists the `--doc` paths as given — not canonicalized (`render.render_file_inputs`,
  sanitized) — and read‑checks them.
  **Boundary:** the path *labels* shown in the preview are sanitized, but the injected
  file *content* is **not** — sanitizing it would corrupt the reviewed document. (Model
  *output* over that content IS sanitized as of #5; input content is deliberately raw.)

## Repo layout for reviewers

**Committed (the product):** `cadre/` (the package — including its shipped data
under `cadre/data/`: starter fleets, personas, palette example, and the
`cadre-fleet` skill source), `pyproject.toml`, `tests/`, `spikes/`,
`docs/reference/`, `docs/RUNBOOK.md`, `STRATEGY.md`, `README.md`,
`requirements*.txt`, this file. Also committed, as contributor knowledge:
**`docs/solutions/`** — documented solutions to past problems (bugs, patterns,
conventions), by category with YAML frontmatter (`module`, `tags`, `problem_type`),
relevant when implementing or debugging in a documented area; and **`CONCEPTS.md`** —
the shared domain vocabulary, relevant when orienting to the code.

**Gitignored (internal process, NOT in the tree you review):** `docs/brainstorms/`,
`docs/plans/`, `docs/specs/`, `CLAUDE.md`, `.claude/`, `.codex/`, `.venv/`.
Don't expect planning/design docs in a review — the committed tree *is* the tool.
