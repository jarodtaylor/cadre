# Hermes / AIAgent reference (authoritative — verify here, don't guess)

This project integrates with **Hermes's `AIAgent`** library (NousResearch/hermes-agent)
through one adapter, `fleet_engine/model_client.py`. `AIAgent` is pre‑1.0 and
**volatile** (calendar‑versioned; `v2026.6.5` observed), so any claim about its
constructor params, toolset names, timeouts, or provider resolution **must be
checked against the sources below — never against model training data.** Guessing
here has already produced a real privilege bug (see "Verified facts" #1).

## Vendored snapshots (offline, for repo‑reading agents)

| File | What | Source | Pinned |
|---|---|---|---|
| [`python-library.md`](./python-library.md) | The `AIAgent` Python‑library guide — install, construct, `chat()`, params, thread‑safety | `raw.githubusercontent.com/NousResearch/hermes-agent/<sha>/website/docs/guides/python-library.md` | SHA `f10f7114f90112ae6f4306789db7d30df5ef4fcd` |
| [`llms.txt`](./llms.txt) | The docs index (page titles + canonical URLs) | `https://hermes-agent.nousresearch.com/docs/llms.txt` | fetched 2026‑06‑17 |

Both are **verbatim upstream snapshots** (© Nous Research), captured for offline
agent reference. They drift as Hermes ships — re‑fetch to refresh:

```bash
curl -sSL "https://raw.githubusercontent.com/NousResearch/hermes-agent/<sha-or-main>/website/docs/guides/python-library.md" -o docs/reference/hermes/python-library.md
curl -sSL "https://hermes-agent.nousresearch.com/docs/llms.txt"      -o docs/reference/hermes/llms.txt
```

## Not vendored (too large / too volatile) — fetch live when web is available

- **Full docs**: `https://hermes-agent.nousresearch.com/docs/llms-full.txt` (~2.9 MB,
  63k lines — the entire docs concatenated for LLM consumption). Don't commit it; fetch on demand:
  ```bash
  curl -sSL "https://hermes-agent.nousresearch.com/docs/llms-full.txt" -o /tmp/hermes-llms-full.txt
  ```
- **Docs site / source**: <https://hermes-agent.nousresearch.com/docs> · <https://github.com/NousResearch/hermes-agent>

## Verified facts (point‑in‑time; re‑verify if behavior surprises)

Source‑traced 2026‑06‑17 against `hermes-agent` tag **`v2026.6.5`** (SHA
`d6b9cfa3e1f460f5729553ef42b174da28b765c7`) and the vendored guide. `AIAgent` is
volatile — treat these as "verified then," not "true forever."

1. **`enabled_toolsets` semantics** (`model_tools.py` `_compute_tool_definitions`, the `if enabled_toolsets is not None:` branch). **This is load‑bearing for our security gate:**
   - `None` → **enables EVERY toolset** (the `else` branch iterates `get_all_toolsets()`), including `terminal`, `file`, `browser`, `code_execution`, `computer_use`.
   - `[]` (empty list) → **enables NO tools** (`is not None` is True → empty loop).
   - `["web", ...]` → exactly those (after composite expansion + `disabled_toolsets` subtraction + per‑tool `check_fn`).
   - ⇒ Our adapter passes `enabled_toolsets=list(toolset)` **verbatim** — never `… or None`. An empty/omitted toolset (incl. the synthesizer) must be `[]`, not `None`. (`HERMES_KANBAN_TASK` env can append the non‑privileged `kanban` toolset even to `[]`.)
2. **Toolset privilege classification** (`toolsets.py` `TOOLSETS`). **Privileged** (act on the machine / external systems / spawn agents): `terminal`, `file` (write‑capable), `code_execution`, `computer_use`, `browser`, `debugging` (composite = `web`+`file`+`terminal`), `delegation`. **Safe** (read/search/analyze/generate/reason): `web`, `search`, `x_search`, `vision`, `video`, `image_gen`, `video_gen`, `tts`, `moa`, `todo`, `clarify`, `safe`, plus others. Our config gate (`fleet_engine/config.py` `SAFE_TOOLSETS`) is a **fail‑closed allowlist**: anything not safe — privileged, composite, or unknown — needs `allow_privileged_tools: true`.
3. **Timeouts** (`agent_init.py` / `chat_completion_helpers.py`). **No `timeout` kwarg on the constructor or `chat()`.** Instead: a stale‑response detector (~90 s non‑streaming; ~120 s streaming TTFB + ~180 s idle) **plus** an OpenAI‑client timeout (**default 1800 s**) — both **raise into `chat()`**, so a hung provider self‑terminates and surfaces as a catchable exception (our adapter's `except Exception` turns it into a typed failure). Configure on the host via `HERMES_API_TIMEOUT` (env) or `cli-config.yaml` `providers.<id>.request_timeout_seconds` / `models.<model>.timeout_seconds`. Distinct from `max_iterations` (agent‑loop bound, **default 90**).
4. **Provider / model resolution** (vendored guide). `AIAgent(provider=…, api_key omitted)` inherits OAuth/configured providers from Hermes auth — no per‑call key needed. **OpenRouter** uses the full `vendor/model` slug; **OAuth providers** (`xai`, `openai-codex`, `nous`) use `provider=` + a **bare** model id. The library does **not** auto‑read a default provider — set it explicitly per specialist.
5. **Stateless / parallel** (vendored guide, "Thread safety"). Use `skip_memory=True` + `skip_context_files=True` for stateless runs; **create one `AIAgent` per thread/task, never share across threads.** We use `quiet_mode=True` too. (U1 spike empirically confirms provider inheritance and records the exact exception type on a deliberate failure, on the real host.)
