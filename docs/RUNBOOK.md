# Runbook — deploy and dogfood a fleet on a Hermes host

The engine is built and unit-tested on a dev machine, but it only runs **live** on a host where Hermes is installed and its providers are authenticated (here: the VPS / Mac Mini). This is the ordered checklist to get from "green on dev" to "running a real multi-provider fleet."

> Steps marked **(confirm)** depend on your specific Hermes install — verify the path/command on your host.

## Phase A — on the dev machine

1. **(Recommended) Land the #2 timeout fix first** so you publish a resilient version. A hung provider currently has no per-specialist deadline and can stall the fan-out (Codex finding #2, deferred). Add a per-specialist timeout + a test, then `unittest` green.
2. **Publish to GitHub** (no remote exists yet):
   ```bash
   gh repo create agent-fleet-factory --public --source=. --remote=origin --push
   # or create the repo in the UI, then:
   #   git remote add origin <url> && git push -u origin feat/fleet-engine-mvp
   ```
   The published tree excludes planning docs, `CLAUDE.md`, and `.claude/` per `.gitignore` — intended. `STRATEGY.md`, `README.md`, this runbook, and the code go up.

## Phase B — on the Hermes host

3. **Clone (or pull) the repo:**
   ```bash
   git clone <url> agent-fleet-factory && cd agent-fleet-factory
   ```
4. **Use the Hermes venv's Python** — it already has `hermes-agent` and a compatible Python. Add `pyyaml` to it; the engine needs nothing else:
   ```bash
   ~/.hermes/hermes-agent/venv/bin/pip install pyyaml          # (confirm) venv path
   ```
   Run everything below with `~/.hermes/hermes-agent/venv/bin/python` from the repo root (so `fleet_engine` imports).
5. **Confirm providers + collect model strings.** Verify ≥2 providers are authenticated (≥1 non-Anthropic). Note the exact `(provider, model)` per lane — OpenRouter takes the full `vendor/model` slug; OAuth providers (xai, openai-codex, nous) take `provider=` + a bare model id.
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
