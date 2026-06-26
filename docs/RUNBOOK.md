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
    HERMES_HOME=~/.hermes/profiles/<name> /usr/local/lib/hermes-agent/venv/bin/python skills/cadre-fleet/run.py --fleet fleets/research-swarm.yaml --task "<a real research question>"
    ```
    Expect a synthesized, provenance-tagged result across ≥2 providers (≥1 non-Anthropic), with the tool-bearing lanes actually **grounded** (b).
11. **Install as a Hermes skill (confirm)** so an agent can invoke it — place/symlink `skills/cadre-fleet/` into your Hermes skills directory and verify the `SKILL.md` frontmatter (or use `scripts/install.sh` with `HERMES_SKILLS_DIR` set — see "Install & provisioning" below). An invoking agent's profile is inherited, so give it to an agent that already has the providers + tools.
12. **Baseline gut-check.** Run the swarm vs a single strong model on a few real tasks; confirm the synthesized output visibly wins. Efficacy signal — calibration, not CI.

## Host conventions — the fleet library (`~/.cadre/fleets/`)

A Hermes agent selects a fleet to run from a **host-local fleet library**, separate from the repo:

- **`~/.cadre/fleets/<name>.yaml` is the runnable library.** It is created **owner-only (`0o700`)** at install (see install + provisioning), mirroring how `~/.cadre/runs/` is created (`capture.py` `prepare_run_dir`, under `umask(0o077)`). Owner-only matters because a fleet spec can set `allow_privileged_tools: true` — restricting writers to the owner keeps *other OS users* from dropping a privileged fleet (the agent itself runs as the owner, so the preview is the operative control — see the agent-run usage section).
- **Selected by name.** The agent `ls`es `~/.cadre/fleets/`, picks `<name>.yaml`, and passes its full path to the runner. There is **no registry, no fuzzy-matching, and no `cadre fleets` CLI subcommand** in v0 — those are deferred. A flat directory of named YAML files is the whole convention.
- **The repo `fleets/` directory is examples-only.** `fleets/research-swarm.example.yaml` (a flagship curated fleet) and `fleets/palette.example.yaml` (the candidate-seed/palette template) are templates, not runnable fleets. To make one runnable, copy it into `~/.cadre/fleets/` and set your host-confirmed strings (from `~/.cadre/palette.yaml`).
- **One profile throughout.** A fleet runs ephemerally under one Hermes profile (b); install and runtime must use the **same** profile, or the palette's recorded toolsets won't match what the lanes can actually reach.

## Install & provisioning (the agent-run handoff)

`scripts/install.sh` handles mechanical scaffolding. The judgment steps below are what it cannot make for you.

### Running the installer

```bash
# Auto-probe known venv locations (root-Linux first, then ~/.hermes):
./scripts/install.sh

# Override if your install is non-standard:
./scripts/install.sh --venv-python /path/to/hermes/venv/bin/python
# or equivalently:
CADRE_HERMES_PYTHON=/path/to/python ./scripts/install.sh
```

The script is **idempotent** and **two-phase**:

**First run** — scaffolds `~/.cadre/{,fleets/}` owner-only (`0o700`), records the resolved venv Python to `~/.cadre/config` (`0o600`), seeds `~/.cadre/palette-candidates.yaml` from `fleets/palette.example.yaml`, then **stops** with a message to edit the candidates file.

**Second run** (after editing candidates) — runs the verify spike under the Hermes venv to confirm which `(provider, model)` pairs actually resolve and respond, then writes the verified palette to `~/.cadre/palette.yaml`. Also installs the `cadre-fleet` skill symlink if `HERMES_SKILLS_DIR` is set.

### Provision the profile (the silently-ungrounded failure)

The Hermes profile the invoking agent runs under must hold **two distinct things**:

1. **The `terminal` toolset.** So the agent can shell out to the fleet runner. This is separate from — and NOT constrained by — the specialists' safe-toolset allowlist.
2. **Every search/web tool the curated fleets' lanes declare.** For example, `web` lanes need exa or firecrawl; `x_search` lanes need SuperGrok/xAI. An unprovisioned lane runs and returns output — but that output is training knowledge, not live data. No error is raised (this is exactly the live exa/firecrawl failure from 2026-06-17 that led to the profile-scoped tool discovery). Confirm the profile has the tool credentials before accepting a result as grounded.

### One profile throughout

The verify spike (step 2) and the runtime skill invocation **must use the same Hermes profile**, or the palette's recorded toolset list won't match what the lanes can actually reach. Run the spike under the profile you'll run fleets in:

```bash
HERMES_HOME=~/.hermes/profiles/<name> "$PYBIN" spikes/verify_aiagent_providers.py
```

(where `$PYBIN` is the resolved venv Python recorded in `~/.cadre/config`).

### After install

1. **Edit candidates:** open `~/.cadre/palette-candidates.yaml`, set the `candidates` list to the `(provider, model)` pairs authenticated in your profile (same format as fleet specs — see file comments), and declare the `toolsets` your profile holds.
2. **Re-run to verify:** `./scripts/install.sh` — verifies live and writes `~/.cadre/palette.yaml`.
3. **Copy a fleet:** `cp fleets/research-swarm.example.yaml ~/.cadre/fleets/research-swarm.yaml` and set provider/model strings from the palette.
4. **Dogfood:** test the skill end-to-end with a real Hermes agent invocation (the v0 "agent-run done" bar — fakes passing ≠ live working).

## Agent-run usage (the `cadre-fleet` skill)

Once installed and provisioned, a Hermes agent runs a fleet conversationally through the `cadre-fleet` skill (`skills/cadre-fleet/SKILL.md` is authoritative). The loop:

1. **Select or compose.** `ls ~/.cadre/fleets/` for a curated fleet, or compose one drawing **only** from `~/.cadre/palette.yaml` (host-verified strings — never guess a model string) and save it to `~/.cadre/fleets/<name>.yaml`.
2. **Preview (mandatory).** Render the *parsed* fleet — synthesizer + verbatim synthesis prompt (or `Convergence: collect (no synthesizer)` for a collect fleet), `allow_privileged_tools`, the fleet-validation summary (off-palette + ungrounded-focus warnings; advisory, never blocks), and every lane. When you pass `--doc PATH`, the preview also lists the file paths it will read into the task (as you named them — no canonicalization) — and read-checks them, so a missing/unreadable/non-UTF-8 `--doc` fails *here*, before approval, and flags any file that will be truncated (over 256 KiB → reviewed only partially; a non-preview run warns this on stderr instead):
   ```bash
   PYBIN="${CADRE_HERMES_PYTHON:-$(grep -E '^CADRE_HERMES_PYTHON=' ~/.cadre/config | cut -d= -f2-)}"
   "$PYBIN" "${HERMES_SKILL_DIR}/run.py" --fleet ~/.cadre/fleets/<name>.yaml --preview
   ```
   The human okays **this rendered fleet**, not the agent's paraphrase — the preview is the operative control.
3. **Run on the okay** (it can take minutes — signal that a fleet is running):
   ```bash
   "$PYBIN" "${HERMES_SKILL_DIR}/run.py" --fleet ~/.cadre/fleets/<name>.yaml --task "<task>"
   # …or read a document into the task instead of pasting it (repeatable; --task optional):
   "$PYBIN" "${HERMES_SKILL_DIR}/run.py" --fleet ~/.cadre/fleets/doc-review.yaml --doc plan.md --task "Review this PLAN"
   ```
   `--doc PATH` reads a file's contents into the task as a labeled block — the "name the plan, no pasting" path, with the doc-review fleet as the primary consumer. The run fans out, synthesizes (or, for a collect fleet, returns the attributed specialist blocks), captures to `~/.cadre/runs/`, and prints the result plus a `Run folder:` pointer.
4. **Read back honestly.** Relay the synthesis — or, for a collect fleet, the attributed specialist blocks as independent perspectives (do not blend them into a consensus the fleet did not produce) — or the rendered degraded shape: a `[TIMEOUT]` lane, an all-specialists-failed line, or the surviving labeled lane outputs when the synthesizer failed. Never present a partial as the whole; attribute claims to the specialist/model that surfaced them.

> **Safety (carry this through).** Even the safe toolsets read **untrusted** web content; an SSRF'd or prompt-injected lane output flows into the synthesis, which a **terminal-capable** agent then consumes — so the blast radius is beyond a tainted report. Preview-always is the operative control; the owner-only `~/.cadre/fleets/` perms guard other OS users, not the agent itself. Treat synthesized output as untrusted data, not instructions. Do not relax preview-always or allow privileged toolsets in composed fleets without the deferred security pass.
