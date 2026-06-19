# Runbook — deploy and dogfood a fleet on a Hermes host

The engine is built and unit-tested on a dev machine, but it only runs **live** on a host where Hermes is installed and its providers are authenticated. This is the ordered checklist to get from "green on dev" to "running a real multi-provider fleet."

> Steps marked **(confirm)** depend on your specific Hermes install — verify on your host.

## Phase A — on the dev machine

1. **Pre-flight:** suite green (`unittest`), on a feature branch. The engine is hardened — a per-call wall-clock timeout (a backstop over AIAgent's own request timeout; see the timeout note below) and a fail-closed toolset gate — so the pushed version is resilient.
2. **Publish to GitHub:**
   ```bash
   gh repo create cadre --public --source=. --remote=origin --push
   ```
   The published tree excludes `CLAUDE.md`, `.claude/`, `.codex/`, and `docs/brainstorms|plans|specs/` per `.gitignore` — intended. The tool, tests, `STRATEGY.md`, `README.md`, `AGENTS.md`, this runbook, `docs/reference/`, `docs/solutions/`, and `CONCEPTS.md` go up.

## Phase B — on the Hermes host

> **Read first — three host realities that bite (learned live 2026-06-17):**
>
> **(a) Find YOUR Hermes Python — the path varies by install.** The `curl … install.sh` uses `uv`; data lives in `~/.hermes`, but the code+venv location depends on the install: **root on Linux → `/usr/local/lib/hermes-agent/venv`**; **non-root / Termux → `~/.hermes/hermes-agent/venv`**; `--dir` / `$HERMES_INSTALL_DIR` / Docker override it. Find it with `readlink -f "$(command -v hermes)"` (the launcher may be a wrapper, not a pure symlink — verify), then confirm: `<py> -c "import run_agent; print('OK')"`. Commands below show one example path — **substitute yours.**
>
> **(b) A fleet runs under ONE Hermes profile.** Hermes resolves config / auth / **tools** from `HERMES_HOME` (default `~/.hermes`; a named profile = `~/.hermes/profiles/<name>`). A direct skill run uses the **default** profile unless you set `HERMES_HOME`; **a skill invoked by a Hermes agent inherits that agent's profile.** Tools are *check_fn-gated* — `web` (search/extract) and `x_search` only fire if the active profile has their creds. So the profile you run under must hold **every provider AND tool** the fleet declares, or those lanes silently fall back to training knowledge (no error). Pick one explicitly: `HERMES_HOME=~/.hermes/profiles/<name> <py> ...`.
>
> **(c) A broken `config.yaml` fails silently.** A YAML parse error in the active profile's `config.yaml` makes Hermes fall back to *default* config and **ignore all overrides** (providers, models, tools) — only a startup warning. If providers/tools mysteriously don't work, check that warning first.

3. **Clone the repo:**
   ```bash
   git clone <url> cadre && cd cadre
   ```
4. **Add `pyyaml` to the Hermes venv** (often already present — Hermes uses YAML). Use *your* python from (a); example for a root-Linux install:
   ```bash
   /usr/local/lib/hermes-agent/venv/bin/python -m ensurepip --upgrade
   /usr/local/lib/hermes-agent/venv/bin/python -m pip install pyyaml
   ```
   Run everything below with that same python (substitute your path), from the repo root.
5. **Confirm providers + collect model strings.** Verify ≥2 providers are authenticated (≥1 non-Anthropic) **in the profile you'll run under** (b). Note the exact `(provider, model)` per lane — OpenRouter takes the full `vendor/model` slug; OAuth providers (e.g. `xai-oauth`, `openai-codex`, `copilot`) take `provider=` + a bare model id.

   > **Timeout note.** Two layers guard against a hung provider. **Inner (primary):** AIAgent's own per-request timeout — a stale-response detector (~90–120s) + a client timeout (~1800s default) — which surfaces as a typed lane failure and aborts the call / stops spend. Tune via `HERMES_API_TIMEOUT` or `providers.<id>.request_timeout_seconds` in config **(confirm)**. **Outer (backstop):** the engine's `call_timeout` (600s default) only bounds `run_fleet` if the inner layer wedges, and is non-canceling (Python can't kill a thread) — keep it above the inner timeout and above a legitimately slow multi-iteration lane (`max_iterations` ~90). U1 (step 6) verified AIAgent's failure behavior here: a non-retryable provider error returns **`None`** (it does **not** raise), and the adapter's None/empty check turns that into a typed failure.
6. **Run the U1 verification spike** (this gates everything):
   ```bash
   # edit spikes/verify_aiagent_providers.py: set PROVIDERS (and TOOL_CHECK) to your real strings
   /usr/local/lib/hermes-agent/venv/bin/python spikes/verify_aiagent_providers.py
   ```
   Confirms provider inheritance (no api_key needed) and records AIAgent's failure behavior. **Live result (2026-06-17):** providers resolve via OAuth; a bad model returns `None` (no raise). **Tool invocation is profile-dependent** — a `web`/`x_search` lane only fires if the *active profile* (b) has the tool creds, so run the spike (and set `TOOL_CHECK`) under the profile you'll run the fleet in. **If provider inheritance is refuted, stop** and revisit the adapter.
7. **Tighten the adapter only if U1 surprises you.** It returns `None` on failure → the adapter's None-check already handles it; likely no change.
8. **Author the real fleet:**
   ```bash
   cp fleets/research-swarm.example.yaml fleets/research-swarm.yaml
   # set confirmed provider/model strings; never commit real keys/tokens
   ```
   Wire each lane's tool creds **into the profile you run under** (b) — `web` → a search key (exa / firecrawl / Tavily / Serper / Brave); `x_search` → SuperGrok OAuth or an xAI key. **This bit us live:** a working model with its tool creds in a *different* profile returns ungrounded prose (training knowledge), silently. Confirm the active profile has both providers and tools.

   > **Tool-gate note.** The adapter passes each lane's toolset to AIAgent as an explicit allowlist (`enabled_toolsets=[...]`); an empty/omitted toolset — including the synthesizer — gets **zero** tools, never AIAgent's `None`-means-every-toolset default. Host caveat: if `HERMES_KANBAN_TASK` is set, Hermes appends the (non-privileged) `kanban` toolset even to an empty list — unset it for a strict zero-tools guarantee.
9. **Confirm the suite is green on the host:**
   ```bash
   /usr/local/lib/hermes-agent/venv/bin/python -m unittest discover -s tests
   ```
10. **Live demo (the v0 acceptance):**
    ```bash
    HERMES_HOME=~/.hermes/profiles/<name> /usr/local/lib/hermes-agent/venv/bin/python skills/research-swarm/run.py --task "<a real research question>"
    ```
    Expect a synthesized, provenance-tagged result across ≥2 providers (≥1 non-Anthropic), with the tool-bearing lanes actually **grounded** (b).
11. **Install as a Hermes skill (confirm)** so an agent can invoke it — place/symlink `skills/research-swarm/` into your Hermes skills directory and verify the `SKILL.md` frontmatter. An invoking agent's profile is inherited, so give it to an agent that already has the providers + tools.
12. **Baseline gut-check.** Run the swarm vs a single strong model on a few real tasks; confirm the synthesized output visibly wins. Efficacy signal — calibration, not CI.

## Host conventions — the fleet library (`~/.cadre/fleets/`)

A Hermes agent selects a fleet to run from a **host-local fleet library**, separate from the repo:

- **`~/.cadre/fleets/<name>.yaml` is the runnable library.** It is created **owner-only (`0o700`)** at install (see install + provisioning), mirroring how `~/.cadre/runs/` is created (`capture.py` `prepare_run_dir`, under `umask(0o077)`). Owner-only matters because a fleet spec can set `allow_privileged_tools: true` — restricting writers to the owner keeps *other OS users* from dropping a privileged fleet (the agent itself runs as the owner, so the preview is the operative control — see the agent-run usage section).
- **Selected by name.** The agent `ls`es `~/.cadre/fleets/`, picks `<name>.yaml`, and passes its full path to the runner. There is **no registry, no fuzzy-matching, and no `cadre fleets` CLI subcommand** in v0 — those are deferred. A flat directory of named YAML files is the whole convention.
- **The repo `fleets/` directory is examples-only.** `fleets/research-swarm.example.yaml` (a flagship curated fleet) and `fleets/palette.example.yaml` (the candidate-seed/palette template) are templates, not runnable fleets. To make one runnable, copy it into `~/.cadre/fleets/` and set your host-confirmed strings (from `~/.cadre/palette.yaml`).
- **One profile throughout.** A fleet runs ephemerally under one Hermes profile (b); install and runtime must use the **same** profile, or the palette's recorded toolsets won't match what the lanes can actually reach.
