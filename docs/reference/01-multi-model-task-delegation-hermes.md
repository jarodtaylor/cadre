> [!PROBLEM STATEMENT] 
> **Static, Dual-Model Limitation in Multi-Agent Delegation**
> The Hermes framework restricts LLM routing to a maximum of two concurrent model configurations: a single "main" agent model and a single "delegation" model configured in `config.yaml`. There is currently no native mechanism to dynamically pass a specific model to the `delegate_task` command, nor is there support for model-routing profiles based on task type. 
> 
> This prevents the orchestration of heterogeneous, multi-model subagent workflows (e.g., simultaneously dispatching specialized research tasks to Grok, Gemini, GPT-4o, and Sonnet, and synthesizing them under a main controller).
>
> ---
> **Key Technical Constraints:**
> * **Lack of Parameterization:** The `delegate_task` command does not accept a `model` argument at runtime.
> * **Rigid Configuration:** The `config.yaml` file allows only one global delegation model, preventing dynamic, task-based routing.
> * **Agent-Level Bottlenecks:** While Hermes supports distinct "Profiles" with separate gateways and memories, each profile remains bound to a static main/delegation dual-model hierarchy.
> 
> **Explored Angles of Resolution:**
> 1. **Custom Hermes Plugins:** Developing a plugin to programmatically intercept and override model routing during delegation.
> 2. **Skill Chaining (Python Scripts):** Writing custom skills that handle model-specific APIs independently.
> 3. **Hermes as a Python Library:** Initializing and configuring multiple distinct agent instances directly in Python to programmatically route subtasks to different models, bypassing the standard `delegate_task` configuration limitations.



### Current Accurate State (Hermes Agent v0.16.0, June 2026)

- **delegate_task**: Spawns isolated child `AIAgent` instances with fresh context. You pass `goal` + `context`. Sub-agents get a restricted toolset. Model is controlled **only** by the parent's `delegation.model` / `delegation.provider` in its `config.yaml` (or inherits parent's model). No per-task override. Parallel up to ~3 by default; supports nested orchestration with `role="orchestrator"`.

- **Profiles**: Excellent for persistent, stateful "expert" agents. Each has its own full `config.yaml` (different default model + provider), `.env`, `SOUL.md`, memory, sessions, skills, and gateway. Fully isolated. CLI: `hermes profile create researcher`, `hermes -p researcher chat ...`, or aliases. Great for long-running specialists; less ideal for ad-hoc "use 4 different models on one research task" without extra orchestration.

- **AIAgent Python library** (`from run_agent import AIAgent`): This is your **strongest tool** for exactly what you want. Each instance is independent, can specify its own `model`, `enabled_toolsets`/`disabled_toolsets`, `quiet_mode=True`, `skip_memory=True` (stateless), etc. Perfect for parallel specialists with different models. Install via `pip install git+https://github.com/NousResearch/hermes-agent.git`.

- **Skills system**: Very powerful. Agents can auto-create skills via `skill_manage`. You can create custom skills with `SKILL.md` + `scripts/` (Python files). Skills can use `terminal` or `execute_code` tools and receive environment variables (API keys). Ideal for wrapping your orchestration logic into something you can invoke naturally from chat ("Research this topic with the multi-expert pipeline").

- **Memory**: Persistent per-agent/profile with search/summarization/skill learning. No native cross-profile shared bank.

### Best Options to 10x Your Multi-Model Hermes Workflows

Here are the practical, accurate paths, ranked by power and fit for your described research pipeline (Grok for X/social, Gemini Flash for web, Sonnet/GPT-class for synthesis, etc.).

#### Option 1: Programmatic Orchestration with AIAgent (Strongly Recommended for Research Pipelines)
This bypasses `delegate_task` limitations entirely. You get **per-subtask model + toolset control**, parallel execution, custom routing logic (e.g., complexity → model choice), and full synthesis in one script.

**Implementation Blueprint** (refined & accurate):

Create a reusable Python script (or wrap it as a Hermes skill—see below).

```python
import concurrent.futures
from run_agent import AIAgent

def run_multi_expert_research(query: str) -> str:
    # Specialist 1: Grok-4 for real-time X/social (strong native tools or via provider)
    grok_agent = AIAgent(
        model="xai/grok-4",  # or your exact OpenRouter slug
        quiet_mode=True,
        enabled_toolsets=["web", "x", "browser"],  # adjust to available
        skip_memory=True,  # or False if you want it to remember
    )
    
    # Specialist 2: Gemini 3 Flash for cheap/fast web research
    gemini_agent = AIAgent(
        model="google/gemini-3.5-flash",  # or current equivalent
        quiet_mode=True,
        enabled_toolsets=["web", "browser"],
        skip_memory=True,
    )
    
    # Specialist 3: Sonnet 4.6 for deeper analysis/coding if needed
    sonnet_agent = AIAgent(
        model="anthropic/claude-sonnet-4.6",
        quiet_mode=True,
        enabled_toolsets=["web", "code", "file"],
        skip_memory=True,
    )
    
    def run_specialist(agent, task_prompt):
        return agent.chat(task_prompt)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(run_specialist, grok_agent, f"Search X and social for latest on: {query}"): "social",
            executor.submit(run_specialist, gemini_agent, f"Find primary sources, papers, and web data on: {query}"): "web",
            executor.submit(run_specialist, sonnet_agent, f"Deep analysis and structured extraction for: {query}"): "analysis",
        }
        results = {label: future.result() for future, label in futures.items()}
    
    # Synthesis with your main powerful model (GPT-5.5 / best reasoning model)
    synthesizer = AIAgent(
        model="openai/gpt-5.5",  # or your Pro subscription model slug
        quiet_mode=True,
        enabled_toolsets=["web"],  # or whatever needed for final pass
    )
    
    synthesis_prompt = f"""Synthesize the following research into a grounded, well-cited report on "{query}".
    
Social/X findings:
{results['social']}

Web/primary sources:
{results['web']}

Deep analysis:
{results['analysis']}

Prioritize primary sources, note conflicts, and provide actionable insights."""
    
    final = synthesizer.chat(synthesis_prompt)
    return final

# Example usage
if __name__ == "__main__":
    print(run_multi_expert_research("latest developments in agentic AI frameworks June 2026"))
```

**Why this 10x-es you**:
- True per-task model selection.
- Parallel execution (much faster than sequential `delegate_task`).
- Custom logic (route cheap tasks to Flash, hard reasoning to Sonnet/GPT-5.5, X-specific to Grok).
- No config.yaml restrictions.
- Easy to extend (add cost tracking, retry logic, complexity classifier before choosing model, etc.).

**Integration with Hermes** (so you can invoke from chat):
- **Best**: Create a custom skill `multi_expert_research` with `SKILL.md` that describes the workflow and a `scripts/orchestrate.py` containing the above logic (or a wrapper). The skill procedure uses `terminal` or `execute_code` to run it, passing the query and your API keys via `required_environment_variables`. Hermes' skills system supports exactly this pattern.
- Alternative: Put the script in your PATH and have Hermes call it via the `terminal` tool when you say the trigger phrase. Or use `execute_code` for simpler inline versions.
- You can also run the script standalone or from any environment and paste results back (or have Hermes read the output file).

This is the closest thing to the "custom plugin" or "skill chaining with py scripts" you mentioned, and it leverages the official Python library guide you already linked.

#### Option 2: Expert Profiles + Orchestration Layer
Create persistent specialized profiles:

- `research-gemini`: Cheap/fast Gemini model + web/browser tools enabled.
- `social-grok`: Grok model + X/web tools.
- `analyst-sonnet`: Sonnet 4.6 + code/file tools.
- `orchestrator-gpt`: Your main powerful model.

**How to use**:
- Main/orchestrator profile can delegate simple tasks (using its global `delegation.model` for cheap sub-work).
- For true multi-model on one task: Use the Python orchestrator above (which can even target different profiles indirectly if needed) or have the orchestrator profile call other profiles via `terminal` (`hermes -p social-grok chat "one-shot task: ..."`) and collect outputs.
- Kanban-style routing via profile descriptions exists as a lighter-weight mechanism.

**Strength**: Persistent memory and state per expert. Long-running "team members" you can talk to directly.
**Weakness**: More overhead for one-off research pipelines; delegation model still global per profile.

#### Option 3: Dynamic / Skill-Injected (Staying Mostly in One Session)
- In your main chat, tell Hermes to use `execute_code` (or a pre-built skill) to write/run a mini-orchestrator Python snippet on the fly (similar to Option 1 but generated dynamically).
- Pre-create a reusable "Multi-Expert Research" skill that encapsulates the pattern.
- This avoids leaving the main session but is less robust than a well-tested external script for complex pipelines.

### Additional Recommendations
- **Track the feature request**: Multiple GitHub issues exist for per-task model/provider overrides in `delegate_task`. When implemented, it will make native delegation much more powerful.
- **Providers**: Use OpenRouter (or direct) for easy model string consistency across Grok, Gemini, Claude, OpenAI, etc.
- **Cost & Speed Optimization**: The Python approach lets you intelligently route (Flash/Gemini for bulk gathering, frontier models only for synthesis).
- **Memory & Grounding**: Pass rich `context` to sub-agents. Use Hermes' built-in memory search or have specialists write structured outputs to files/databases for the synthesizer to reference.
- **Self-improvement**: Once you have a working pipeline, let Hermes observe it and auto-create/refine a skill from the pattern (its core strength).
- **Testing**: Start with the standalone Python script to validate the multi-model flow, then wrap it as a skill for seamless Hermes integration.

This approach gives you far more control and flexibility than native `delegate_task` alone. The Python library + skills combination is the real "10x" lever here—exactly as the official docs intend for advanced orchestration.

