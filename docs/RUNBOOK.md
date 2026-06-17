# Runbook — deploy and dogfood a fleet on a Hermes host

The engine is built and unit-tested on a dev machine, but it only runs **live** on a host where Hermes is installed and its providers are authenticated (here: the VPS / Mac Mini). This is the ordered checklist to get from "green on dev" to "running a real multi-provider fleet."

> Steps marked **(confirm)** depend on your specific Hermes install — verify the path/command on your host.

## Phase A — on the dev machine

1. **#2 timeout fix — done.** Every model call (each specialist *and* the synthesizer) is bounded by an outer wall-clock timeout on a daemon thread (`run_fleet(call_timeout=...)`, default 600s), so a hung provider can't stall the fan-out or block process exit (commits `7877ffa`; allowlist security follow-up `6ffe711`). It's a *backstop* over AIAgent's own request timeout — see the **timeout note** under Phase B before relying on it unattended.
2. **Publish to GitHub** (no remote exists yet):
   ```bash
   gh repo create cadre --public --source=. --remote=origin --push
   # or create the repo in the UI, then:
   #   git remote add origin <url> && git push -u origin feat/fleet-engine-mvp
   ```
   The published tree excludes planning docs, `CLAUDE.md`, and `.claude/` per `.gitignore` — intended. `STRATEGY.md`, `README.md`, this runbook, and the code go up.

## Phase B — on the Hermes host

3. **Clone (or pull) the repo:**
   ```bash
   git clone <url> cadre && cd cadre
   ```
4. **Use the Hermes venv's Python** — it already has `hermes-agent` and a compatible Python. Add `pyyaml` to it; the engine needs nothing else:
   ```bash
   ~/.hermes/hermes-agent/venv/bin/pip install pyyaml          # (confirm) venv path
   ```
   Run everything below with `~/.hermes/hermes-agent/venv/bin/python` from the repo root (so `fleet_engine` imports).
5. **Confirm providers + collect model strings.** Verify ≥2 providers are authenticated (≥1 non-Anthropic). Note the exact `(provider, model)` per lane — OpenRouter takes the full `vendor/model` slug; OAuth providers (xai, openai-codex, nous) take `provider=` + a bare model id.

   > **Timeout note.** Two layers guard against a hung provider. **Inner (primary):** AIAgent's own per-request timeout — a stale-response detector (~90–120s) + a client timeout (~1800s default) — raises into `chat()` and surfaces as a typed lane failure; this is what actually aborts the network call and stops provider spend. Tune it on the host via `HERMES_API_TIMEOUT` (env) or `providers.<id>.request_timeout_seconds` in `cli-config.yaml` **(confirm)**. **Outer (backstop):** the engine's `call_timeout` (600s default) only bounds `run_fleet` if the inner layer wedges, and is non-canceling (Python can't kill a thread) — keep it above the inner timeout and above a legitimately slow multi-iteration lane (`max_iterations` defaults to ~90). U1 (step 6) verifies the load-bearing assumption that AIAgent raises a **catchable** exception on a hung/failed call.
6. **Run the U1 verification spike** (this gates everything):
   ```bash
   # edit spikes/verify_aiagent_providers.py: set PROVIDERS (and TOOL_CHECK) to your real strings
   ~/.hermes/hermes-agent/venv/bin/python spikes/verify_aiagent_providers.py
   ```
   Confirms: provider inheritance works (no api_key needed), a tool-enabled specialist actually invokes its tool, and records the **exact exception type** `AIAgent` raises on failure. **If provider inheritance is refuted, stop** and revisit the U2 adapter before going further.
7. **Tighten U2 if needed** using the exception type U1 recorded (the adapter catch is already broad, so likely no change — confirm).
8. **Author the real fleet:**
   ```bash
   cp fleets/research-swarm.example.yaml fleets/research-swarm.yaml
   # set confirmed provider/model strings; never commit real keys/tokens
   ```
   Wire the search-tool credentials each lane needs — `web` → a search key (Tavily/Serper/Brave); `x_search` → SuperGrok OAuth or an xAI key. A specialist with a working model but an unauthed tool returns ungrounded prose.

   > **Tool-gate note.** The adapter passes each lane's toolset to AIAgent as an explicit allowlist (`enabled_toolsets=[...]`); an empty/omitted toolset — including the synthesizer — gets **zero** tools, never AIAgent's `None`-means-every-toolset default. One host caveat: if `HERMES_KANBAN_TASK` is set in the fleet's runtime environment, Hermes appends the (non-privileged) `kanban` toolset even to an empty list — unset it for a strict zero-tools guarantee.
9. **Confirm the suite is green on the host:**
   ```bash
   ~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests
   ```
10. **Live demo (the v0 acceptance):**
    ```bash
    ~/.hermes/hermes-agent/venv/bin/python skills/research-swarm/run.py --task "<a real research question>"
    ```
    Expect a synthesized, provenance-tagged result across ≥2 providers (≥1 non-Anthropic).
11. **Install as a Hermes skill (confirm)** so it's invokable from Hermes — place or symlink `skills/research-swarm/` into your Hermes skills directory, and verify the `SKILL.md` frontmatter against your Hermes version.
12. **Baseline gut-check.** Run the swarm vs a single strong model on a few real tasks; confirm the synthesized output visibly wins. This is the efficacy signal — calibration, not CI.
