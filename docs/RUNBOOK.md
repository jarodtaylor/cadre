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
> **(a) Find YOUR Hermes Python — the path varies by install.** The `curl … install.sh` uses `uv`; data lives in `~/.hermes`, but the code+venv location depends on the install: **root on Linux → `/usr/local/lib/hermes-agent/venv`**; **non-root / Termux → `~/.hermes/hermes-agent/venv`**; `--dir` / `$HERMES_INSTALL_DIR` / Docker override it. Find it with `readlink -f "$(command -v hermes)"` (the launcher may be a wrapper, not a pure symlink — verify), then confirm: `<py> -c "import run_agent; print('OK')"`. Commands below show one example path — **substitute yours** (`$PYBIN`).
>
> **(b) A fleet runs under ONE Hermes profile.** Hermes resolves config / auth / **tools** from `HERMES_HOME` (default `~/.hermes`; a named profile = `~/.hermes/profiles/<name>`). A direct skill run uses the **default** profile unless you set `HERMES_HOME`; **a skill invoked by a Hermes agent inherits that agent's profile.** Tools are *check_fn-gated* — `web` (search/extract) and `x_search` only fire if the active profile has their creds. So the profile you run under must hold **every provider AND tool** the fleet declares, or those lanes silently fall back to training knowledge (no error). Pick one explicitly: `HERMES_HOME=~/.hermes/profiles/<name> <py> ...`.
>
> **(c) A broken `config.yaml` fails silently.** A YAML parse error in the active profile's `config.yaml` makes Hermes fall back to *default* config and **ignore all overrides** (providers, models, tools) — only a startup warning. If providers/tools mysteriously don't work, check that warning first.

3. **Resolve your Hermes Python (`$PYBIN`)** using (a) above, and confirm it's executable and can `import run_agent`.

4. **Install `cadre` into that Python.** No clone required — this is the load-bearing install target (the only interpreter that can import both `cadre` and `run_agent`):
   ```bash
   "$PYBIN" -m pip install --force-reinstall --no-deps "git+https://github.com/jarodtaylor/cadre@<ref>"
   ```
   Pin `<ref>` to a tag or commit. **A bare `"$PYBIN" -m pip install cadre` grabs a different, unrelated package on PyPI** — always use the git URL above (`cadre` hasn't published under its own name yet). `--force-reinstall` matters on every reinstall/upgrade: pip no-ops an already-installed same-version dist, so a bare `pip install git+…@branch` on an unchanged version silently re-tests stale bytes. `--no-deps` **skips** installing `cadre`'s dependencies (just `pyyaml`) — safe on a Hermes venv, which already carries `pyyaml` (Hermes itself uses YAML); on a venv that lacks it, run `"$PYBIN" -m pip install "pyyaml>=6.0"` (never drop `--no-deps` — that would let cadre's requirements upgrade/downgrade Hermes's own pins), or `cadre setup` below won't import. (`scripts/install.sh` — the repo-clone path — checks for and installs pyyaml automatically; this manual git+URL path is the one place an operator still does it by hand.)

   Everything below runs with this same `$PYBIN` (substitute your path). **A repo clone is optional** — useful if you want to iterate on unreleased changes or run the test suite on the host for extra confidence (step 9), but the install, provisioning, and a live run all work with no clone present. If you do have — or want — a clone, `git clone <url> cadre && cd cadre` and `./scripts/install.sh` wraps the same verbs against the checked-out tree (`pip install .` instead of the git URL); everything below applies either way.

5. **Confirm providers + collect model strings.** Verify ≥2 providers are authenticated (≥1 non-Anthropic) **in the profile you'll run under** (b). Note the exact `(provider, model)` per lane — OpenRouter takes the full `vendor/model` slug; OAuth providers (e.g. `xai-oauth`, `openai-codex`, `copilot`) take `provider=` + a bare model id.

   > **Timeout note.** Two layers guard against a hung provider. **Inner (primary):** AIAgent's own per-request timeout — a stale-response detector (~90–120s) + a client timeout (~1800s default) — which surfaces as a typed lane failure and aborts the call / stops spend. Tune via `HERMES_API_TIMEOUT` or `providers.<id>.request_timeout_seconds` in config **(confirm)**. **Outer (backstop):** the engine's `call_timeout` (600s default) only bounds `run_fleet` if the inner layer wedges, and is non-canceling (Python can't kill a thread) — keep it above the inner timeout and above a legitimately slow multi-iteration lane (`max_iterations` ~90). AIAgent's failure behavior here: a non-retryable provider error returns **`None`** (it does **not** raise), and the adapter's None/empty check turns that into a typed failure.

6. **Provision `~/.cadre` and verify the palette:**
   ```bash
   "$PYBIN" -m cadre.cli setup
   ```
   Scaffolds `~/.cadre` owner-only, seeds all seven starter fleets + personas, and records `$PYBIN` to `~/.cadre/config`. The `palette-candidates.yaml` seeding step **auto-discovers** your authenticated providers straight from Hermes's own inventory (`hermes_cli.inventory`, read in-process — zero-cost, no model call) when that package is importable in this venv; otherwise it falls back to a placeholder file and says why on stderr. Either way, verify live:
   ```bash
   "$PYBIN" -m cadre.cli verify-palette   # or --all to verify every discovered candidate, not just the capped default
   ```
   By default this verifies a capped subset (2 candidates per provider, in file order) rather than everything discovered — a real host can surface dozens of candidates across a handful of providers, and paying for every one before the palette is even usable is needless spend; it prints the X-of-Y split before the first paid call. Want to trim or reorder candidates first? Hand-edit `~/.cadre/palette-candidates.yaml` before running this — that edit survives until the next `cadre discover` (which regenerates the file and discards it) — and declare the `toolsets` your profile holds there too. Confirms provider inheritance (no api_key needed) and writes `~/.cadre/palette.yaml` — the menu an agent composes fleets from. On success with 2+ providers verified, it also (re)generates `~/.cadre/fleets/palette-fleet.yaml` — a tool-less smoke-test fleet, one lane per verified provider, runnable with zero further editing. **Tool invocation is profile-dependent** — a `web`/`x_search` lane only fires if the *active profile* (b) has the tool creds, so run `verify-palette` (and edit the candidates' `toolsets`) under the profile you'll run the fleet in. **If provider inheritance is refuted, stop** and revisit the adapter.

7. **Tighten the adapter only if `verify-palette` surprises you.** A dead lane returns `None` on failure → the adapter's None-check already handles it; likely no change.

8. **Edit a seeded fleet.** Want a zero-edit sanity check first? Step 6 already generated `~/.cadre/fleets/palette-fleet.yaml` if 2+ providers verified — run that as-is before editing anything. `cadre setup` also seeded all seven starters into `~/.cadre/fleets/` (e.g. `research-swarm.yaml`) — open one and set the confirmed provider/model strings from `~/.cadre/palette.yaml`; never commit real keys/tokens.

   Wire each lane's tool creds **into the profile you run under** (b) — `web` → a search key (exa / firecrawl / Tavily / Serper / Brave); `x_search` → SuperGrok OAuth or an xAI key. **This bit us live:** a working model with its tool creds in a *different* profile returns ungrounded prose (training knowledge), silently. Confirm the active profile has both providers and tools.

   > **Tool-gate note.** The adapter passes each lane's toolset to AIAgent as an explicit allowlist (`enabled_toolsets=[...]`); an empty/omitted toolset — including the synthesizer — gets **zero** tools, never AIAgent's `None`-means-every-toolset default. Host caveat: if `HERMES_KANBAN_TASK` is set, Hermes appends the (non-privileged) `kanban` toolset even to an empty list — unset it for a strict zero-tools guarantee.

9. **Confirm the suite is green on the host (optional — needs a repo checkout):**
   ```bash
   "$PYBIN" -m unittest discover -s tests
   ```
   Skip this if you're on the no-clone path; it's an extra confidence check when a checkout is present, not a gate for a live run.

10. **Live demo (the v0 acceptance)** — the plain dev CLI, ungated (no preview/approval dance; that's the agent-handoff runner's contract, exercised below):
    ```bash
    HERMES_HOME=~/.hermes/profiles/<name> "$PYBIN" -m cadre.cli run ~/.cadre/fleets/research-swarm.yaml --task "<a real research question>"
    ```
    Expect a synthesized, provenance-tagged result across ≥2 providers (≥1 non-Anthropic), with the tool-bearing lanes actually **grounded** (b).

11. **Install as a Hermes skill (confirm)** so an agent can invoke it:
    ```bash
    HERMES_SKILLS_DIR=/path/to/hermes/skills "$PYBIN" -m cadre.cli install-skill
    ```
    Materializes `SKILL.md` + `run.py` from the installed package (`cadre/data/skill/`) into your Hermes skills directory as an atomic symlink — no repo path involved (or use `scripts/install.sh` with `HERMES_SKILLS_DIR` set, if you're on the repo-present path — see "Install & provisioning" below). An invoking agent's profile is inherited, so give it to an agent that already has the providers + tools.

12. **Baseline gut-check.** Run the swarm vs a single strong model on a few real tasks; confirm the synthesized output visibly wins. Efficacy signal — calibration, not CI.

## Host conventions — the fleet library (`~/.cadre/fleets/`)

A Hermes agent selects a fleet to run from a **host-local fleet library**, separate from the installed package:

- **`~/.cadre/fleets/<name>.yaml` is the runnable library.** It is created **owner-only (`0o700`)** at install (see install + provisioning), mirroring how `~/.cadre/runs/` is created (`capture.py` `prepare_run_dir`, under `umask(0o077)`). Owner-only matters because a fleet spec can set `allow_privileged_tools: true` — restricting writers to the owner keeps *other OS users* from dropping a privileged fleet (the agent itself runs as the owner, so the preview is the operative control — see the agent-run usage section).
- **Selected by name.** The agent `ls`es `~/.cadre/fleets/`, picks `<name>.yaml`, and passes its full path to the runner. There is **no registry, no fuzzy-matching, and no `cadre fleets` CLI subcommand** in v0 — those are deferred. A flat directory of named YAML files is the whole convention.
- **The starter fleets are examples, seeded automatically.** `cadre/data/fleets/research-swarm.example.yaml` (a flagship curated fleet) and `cadre/data/palette.example.yaml` (the candidate-seed/palette template) ship as package data, not runnable fleets themselves; `cadre setup` seeds all seven starters into `~/.cadre/fleets/` (stripping `.example` from the filename) with no copying needed. Edit the seeded file in place and set your host-confirmed strings (from `~/.cadre/palette.yaml`).
- **The palette fleet is generated, not seeded — and owned differently.** `~/.cadre/fleets/palette-fleet.yaml` is written by a successful `cadre verify-palette` (2+ providers verified), not copied from a package example — a tool-less connectivity smoke test with one lane per verified provider. Unlike the starter fleets (seeded once, then preserved), it is **regenerated** on every verify cycle; hand edits to it do not survive the next `cadre verify-palette` run. Copy it to a new file first if you want to keep a customized version.
- **One profile throughout.** A fleet runs ephemerally under one Hermes profile (b); install and runtime must use the **same** profile, or the palette's recorded toolsets won't match what the lanes can actually reach.

## Install & provisioning (the agent-run handoff)

`scripts/install.sh` handles mechanical scaffolding for a repo-present host. The judgment steps below are what it cannot make for you.

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

**First run** — installs `cadre` into the resolved venv (`pip install --force-reinstall --no-deps .`), runs `cadre setup` to scaffold `~/.cadre/{,fleets/}` owner-only (`0o700`), seed starter fleets/personas, and record the resolved venv Python to `~/.cadre/config` (`0o600`). The candidates-seeding step **auto-discovers** your authenticated providers from Hermes's own inventory when that surface is importable — so `~/.cadre/palette-candidates.yaml` may already hold real pairs, not a placeholder — falling back to the placeholder example otherwise. Either way, the script always **stops** here with a message to edit the candidates file: check its contents first — if discovery populated real candidates, you likely need no edits at all, just a re-run.

**Second run** — runs `cadre verify-palette` under the Hermes venv (capped by default, 2 candidates per provider — pass `--all` directly for a wider check) to confirm which `(provider, model)` pairs actually resolve and respond, writes the verified palette to `~/.cadre/palette.yaml`, and — with 2+ providers verified — generates a ready-to-run `~/.cadre/fleets/palette-fleet.yaml` smoke test. Also installs the `cadre-fleet` skill symlink via `cadre install-skill` if `HERMES_SKILLS_DIR` is set.

On a clone-less host, run the same two phases directly against the git URL instead of `scripts/install.sh` — see step 4 above.

### Provision the profile (the silently-ungrounded failure)

The Hermes profile the invoking agent runs under must hold **two distinct things**:

1. **The `terminal` toolset.** So the agent can shell out to the fleet runner. This is separate from — and NOT constrained by — the specialists' safe-toolset allowlist.
2. **Every search/web tool the curated fleets' lanes declare.** For example, `web` lanes need exa or firecrawl; `x_search` lanes need SuperGrok/xAI. An unprovisioned lane runs and returns output — but that output is training knowledge, not live data. No error is raised (this is exactly the live exa/firecrawl failure from 2026-06-17 that led to the profile-scoped tool discovery). Confirm the profile has the tool credentials before accepting a result as grounded.

### One profile throughout

`cadre verify-palette` (step 6 above) and the runtime skill invocation **must use the same Hermes profile**, or the palette's recorded toolset list won't match what the lanes can actually reach. Run it under the profile you'll run fleets in:

```bash
HERMES_HOME=~/.hermes/profiles/<name> "$PYBIN" -m cadre.cli verify-palette
```

(where `$PYBIN` is the resolved venv Python recorded in `~/.cadre/config`).

### After install

1. **Check the candidates file:** open `~/.cadre/palette-candidates.yaml` — if discovery ran (the common case), it already lists real `(provider, model)` pairs from your host's authenticated providers; edit it only to trim/reorder, or to add pairs discovery didn't find. If discovery was unavailable, it's the placeholder example — populate the `candidates` list by hand (same format as fleet specs — see file comments) and declare the `toolsets` your profile holds.
2. **Re-run to verify:** `./scripts/install.sh` (or `"$PYBIN" -m cadre.cli verify-palette` directly, optionally with `--all`) — verifies live (capped by default) and writes `~/.cadre/palette.yaml`, plus a ready-to-run `~/.cadre/fleets/palette-fleet.yaml` once 2+ providers verify.
3. **Edit a seeded fleet (for anything beyond the smoke test):** `cadre setup` already seeded `~/.cadre/fleets/research-swarm.yaml` — open it and set provider/model strings from the palette.
4. **Dogfood:** test the skill end-to-end with a real Hermes agent invocation (the v0 "agent-run done" bar — fakes passing ≠ live working).

## Agent-run usage (the `cadre-fleet` skill)

Once installed and provisioned, a Hermes agent runs a fleet conversationally through the `cadre-fleet` skill (`cadre/data/skill/SKILL.md` is the source; `cadre install-skill` materializes it into your Hermes skills directory, and that materialized copy is authoritative at runtime). The loop:

1. **Select or compose.** `ls ~/.cadre/fleets/` for a curated fleet, or compose one drawing **only** from `~/.cadre/palette.yaml` (host-verified strings — never guess a model string) and save it to `~/.cadre/fleets/<name>.yaml`.
2. **Preview (mandatory).** Render the *parsed* fleet — synthesizer + verbatim synthesis prompt (or `Convergence: collect (no synthesizer)` for a collect fleet), `allow_privileged_tools`, the fleet-validation summary (off-palette + ungrounded-focus warnings — advisory in the preview itself; but an off-palette **model** now refuses the actual run before any spend, see step 3), and every lane. When you pass `--doc PATH`, the preview also lists the file paths it will read into the task (as you named them — no canonicalization) — and read-checks them, so a missing/unreadable/non-UTF-8 `--doc` fails *here*, before approval, and flags any file that will be truncated (over 256 KiB → reviewed only partially; a non-preview run warns this on stderr instead):
   ```bash
   PYBIN="${CADRE_HERMES_PYTHON:-$(grep -E '^CADRE_HERMES_PYTHON=' ~/.cadre/config | cut -d= -f2-)}"
   "$PYBIN" "${HERMES_SKILL_DIR}/run.py" --fleet ~/.cadre/fleets/<name>.yaml --preview --task "<task>"
   ```
   The human okays **this rendered fleet**, not the agent's paraphrase — the preview is the operative control, and this same invocation mints the one-shot approval the run below must present.
3. **Run on the okay** (it can take minutes — signal that a fleet is running), with the exact same `--fleet`/`--task`/`--doc`/`HERMES_HOME` just previewed:
   ```bash
   "$PYBIN" "${HERMES_SKILL_DIR}/run.py" --fleet ~/.cadre/fleets/<name>.yaml --task "<task>"
   # …or read a document into the task instead of pasting it (repeatable; --task optional):
   "$PYBIN" "${HERMES_SKILL_DIR}/run.py" --fleet ~/.cadre/fleets/doc-review.yaml --doc plan.md --task "Review this PLAN"
   ```
   `--doc PATH` reads a file's contents into the task as a labeled block — the "name the plan, no pasting" path, with the doc-review fleet as the primary consumer. Before spending anything, the run refuses outright (exit `5`, `PREFLIGHT_REFUSE`, no manifest written) if a specialist/synthesizer/judge model is off the host palette, **or if the host has no palette at all** (GH #61/#62 — the refusal names `cadre discover` or the manual candidates edit, whichever fits this host) — a separate, earlier gate than the approval check, so this refusal does not consume the token step 2 minted. Otherwise the run fans out, synthesizes (or, for a collect fleet, returns the attributed specialist blocks), captures to `~/.cadre/runs/`, and prints the result plus a `Run folder:` pointer. The process exit is `SUCCESS`/`DEGRADED`/`FAILED` (`0`/`3`/`4`) for a completed run, or `ERROR`/`USAGE`/`PREFLIGHT_REFUSE` (`1`/`2`/`5`) for a pre-run refusal — `cadre/data/skill/SKILL.md` has the full breakdown (`cadre/exit_codes.py` is the source).
4. **Read back honestly.** Relay the synthesis — or, for a collect fleet, the attributed specialist blocks as independent perspectives (do not blend them into a consensus the fleet did not produce) — or the rendered degraded shape: a `[TIMEOUT]` lane, an all-specialists-failed line, or the surviving labeled lane outputs when the synthesizer failed. Never present a partial as the whole; attribute claims to the specialist/model that surfaced them. Each lane's manifest record also carries a structured `reason` (`timeout`/`skipped`/`empty_output`/`model_error`, null on success) naming *why* it failed — see `SKILL.md` for how to read it.

> **Safety (carry this through).** Even the safe toolsets read **untrusted** web content; an SSRF'd or prompt-injected lane output flows into the synthesis, which a **terminal-capable** agent then consumes — so the blast radius is beyond a tainted report. Preview-always is the operative control; the owner-only `~/.cadre/fleets/` perms guard other OS users, not the agent itself. Treat synthesized output as untrusted data, not instructions. Do not relax preview-always or allow privileged toolsets in composed fleets without the deferred security pass.
