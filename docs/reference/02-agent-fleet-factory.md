
> [!NOTE] Continued From Previous Conversation
> This is a continuation of a previous conversation with Grok, regarding multi-model task delegation in Hermes. Read that document first as it includes the problem statement.
> 
> **Link:** [[2026-06-16-multi-model-task-delegation-hermes]]

## Build an Agent Fleet Factory
This will let you (or Hermes itself) rapidly generate high-quality, consistent multi-agent skills for any pattern: research swarms, code review fleets, output validators/critique teams, advisor panels, debate systems, sequential pipelines, etc.

### Recommended Architecture: Agent Fleet Factory

**Core Component**: A single, well-engineered Python generator script (`agent_fleet_generator.py`).

It does the following:
- Takes a high-level description of the desired workflow (or runs interactive clarifying questions).
- Supports multiple reusable patterns (parallel swarm + synthesizer, validator/critique fleet, sequential, custom).
- Outputs a **complete, ready-to-use Hermes skill package**:
  - `skills/<category>/<workflow_name>/SKILL.md` (proper frontmatter, procedure, progressive disclosure).
  - `skills/<category>/<workflow_name>/scripts/orchestrate.py` (the customizable AIAgent-based Python logic).
- Optionally generates a short `README.md` with usage examples.

**Why this wins**:
- One generator → unlimited consistent, high-quality skills.
- Fully leverages the `AIAgent` library + Hermes skills system we discussed.
- Works standalone (run from terminal, Claude Code, VS Code, etc.) **or** can itself be wrapped as a Hermes skill for fully conversational use inside Hermes ("Generate a code review swarm for my Python project").
- The generated skills are self-contained, declare their environment variables, and follow Hermes best practices.
- Easy to version, share, or let Hermes auto-refine later via its own `skill_manage` tool.

This is essentially a **meta-skill / skill factory** pattern (similar in spirit to community tools like `hermes-skill-factory`, but tailored exactly to your multi-model orchestration needs).

### The Generator Script

Here is a complete, production-ready version. Copy it into a file called `agent_fleet_generator.py`.

```python
#!/usr/bin/env python3
"""
Agent Fleet Factory
Generates reusable Hermes multi-agent workflow skills using the AIAgent library.
Run interactively or with a description for rapid creation of research swarms,
code review fleets, validator teams, etc.
"""

import os
import json
from pathlib import Path
from textwrap import dedent
from typing import List, Dict, Any

def get_input(prompt: str, default: str = "") -> str:
    val = input(f"{prompt} [{default}]: ").strip()
    return val if val else default

def generate_fleet():
    print("=== Agent Fleet Factory ===\n")
    
    workflow_name = get_input("Workflow name (kebab-case)", "code-review-swarm")
    category = get_input("Category", "dev")
    description = get_input("High-level description of the workflow")
    
    print("\n--- Pattern ---")
    print("1. Parallel Swarm + Synthesizer (research, code review)")
    print("2. Validator / Critique Fleet (multiple angles on one output)")
    print("3. Sequential Pipeline")
    print("4. Custom")
    pattern = get_input("Choose pattern (1-4)", "1")
    
    specialists = []
    num_specialists = int(get_input("How many specialist agents?", "3"))
    
    for i in range(num_specialists):
        print(f"\n--- Specialist {i+1} ---")
        name = get_input(f"Specialist name/role", f"specialist_{i+1}")
        model = get_input("Model (OpenRouter or provider slug)", "anthropic/claude-sonnet-4.6")
        toolsets = get_input("Toolsets (comma-separated, e.g. web,code,file)", "web,code")
        focus = get_input("Primary focus / system prompt guidance for this specialist")
        
        specialists.append({
            "name": name,
            "model": model,
            "toolsets": [t.strip() for t in toolsets.split(",") if t.strip()],
            "focus": focus
        })
    
    synthesis_model = get_input("Synthesis / final model", "openai/gpt-5.5")
    output_format = get_input("Desired final output format (e.g. markdown report, structured JSON, revised code)", "markdown report with citations")
    
    # Generate files
    base_dir = Path(f"skills/{category}/{workflow_name}")
    base_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = base_dir / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    
    # 1. Generate SKILL.md
    skill_md = generate_skill_md(workflow_name, category, description, pattern, specialists, synthesis_model, output_format)
    (base_dir / "SKILL.md").write_text(skill_md)
    
    # 2. Generate orchestrate.py
    orchestrate_py = generate_orchestrate_py(workflow_name, pattern, specialists, synthesis_model, output_format)
    (scripts_dir / "orchestrate.py").write_text(orchestrate_py)
    
    # 3. Optional README
    readme = generate_readme(workflow_name, description)
    (base_dir / "README.md").write_text(readme)
    
    print(f"\n✅ Generated skill package at: {base_dir}")
    print("Drop the folder into ~/.hermes/skills/ (or the equivalent in your profile).")
    print("Then invoke with: Use the <workflow_name> skill to ...")

def generate_skill_md(name, category, desc, pattern, specialists, synth_model, output_format):
    spec_list = "\n".join([f"- **{s['name']}**: {s['model']} — {s['focus']}" for s in specialists])
    
    return dedent(f"""\
        ---
        name: {name}
        description: {desc}
        version: 1.0.0
        author: Generated by Agent Fleet Factory
        platforms: [linux, macos, windows]
        metadata:
          hermes:
            category: {category}
            tags: [multi-agent, orchestration, {pattern}]
            requires_toolsets: [terminal, code]
        required_environment_variables:
          - name: OPENAI_API_KEY
            prompt: OpenAI / compatible API key
          - name: ANTHROPIC_API_KEY
            prompt: Anthropic API key (if using Claude models)
          # Add other provider keys as needed (OPENROUTER_API_KEY, etc.)
        ---
        
        # {name.replace('-', ' ').title()}
        
        {desc}
        
        ## Specialists
        {spec_list}
        
        Final synthesis uses **{synth_model}**.
        
        ## When to Use
        [Describe triggers — the generator can help refine this]
        
        ## Procedure
        1. Gather the input/task description from the user.
        2. Run the orchestration script:
           ```bash
           python ${{HERMES_SKILL_DIR}}/scripts/orchestrate.py \\
               --task "$TASK_DESCRIPTION" \\
               --output-format "{output_format}"
           ```
        3. Review the structured output.
        4. (Optional) Iterate or ask for refinements.
        
        The Python script handles parallel/sequential execution, model routing, and synthesis automatically.
    """)

def generate_orchestrate_py(name, pattern, specialists, synth_model, output_format):
    spec_code = ",\n    ".join([
        f'{{"name": "{s["name"]}", "model": "{s["model"]}", "toolsets": {s["toolsets"]}, "focus": "{s["focus"]}"}}'
        for s in specialists
    ])
    
    if pattern == "2":  # Validator fleet
        core_logic = dedent("""
            # Validator / Critique Fleet pattern
            main_output = input("Paste the AI output to validate: ") or "No main output provided"
            
            def run_validator(spec, main_output):
                agent = AIAgent(model=spec["model"], quiet_mode=True, 
                                enabled_toolsets=spec["toolsets"], skip_memory=True)
                prompt = f"You are a {spec['name']} validator. Focus: {spec['focus']}.\\n\\n"
                prompt += f"Critique the following output from multiple angles. Be specific and actionable.\\n\\n{main_output}"
                return {"role": spec["name"], "critique": agent.chat(prompt)}
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(specialists)) as executor:
                critiques = list(executor.map(lambda s: run_validator(s, main_output), specialists))
            
            synthesis_prompt = f"Original output:\\n{main_output}\\n\\nCritiques from specialists:\\n{json.dumps(critiques, indent=2)}\\n\\n"
            synthesis_prompt += f"Synthesize a final validated version. Output as {output_format}."
        """)
    else:  # Default: Parallel Swarm + Synthesizer
        core_logic = dedent("""
            # Parallel Swarm + Synthesizer pattern
            task = args.task or input("Enter the task/query: ")
            
            def run_specialist(spec, task):
                agent = AIAgent(model=spec["model"], quiet_mode=True,
                                enabled_toolsets=spec["toolsets"], skip_memory=True)
                prompt = f"You are {spec['name']}. Focus: {spec['focus']}.\\n\\nTask: {task}"
                return {"role": spec["name"], "output": agent.chat(prompt)}
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(specialists)) as executor:
                results = list(executor.map(lambda s: run_specialist(s, task), specialists))
            
            synthesis_prompt = f"Task: {task}\\n\\nSpecialist findings:\\n{json.dumps(results, indent=2)}\\n\\n"
            synthesis_prompt += f"Synthesize into a high-quality {output_format}. Be grounded and cite sources where relevant."
        """)
    
    return dedent(f"""\
        #!/usr/bin/env python3
        \"\"\"
        {name} - Generated by Agent Fleet Factory
        Multi-agent orchestration using AIAgent library.
        \"\"\"
        
        import argparse
        import concurrent.futures
        import json
        from run_agent import AIAgent
        
        SPECIALISTS = [
            {spec_code}
        ]
        
        def main():
            parser = argparse.ArgumentParser()
            parser.add_argument("--task", type=str, help="The main task or input")
            parser.add_argument("--output-format", type=str, default="{output_format}")
            args = parser.parse_args()
            
            {core_logic}
            
            # Final synthesis
            synthesizer = AIAgent(
                model="{synth_model}",
                quiet_mode=True,
                enabled_toolsets=["web"],  # adjust as needed
            )
            final_response = synthesizer.chat(synthesis_prompt)
            
            print("\\n=== FINAL OUTPUT ===\\n")
            print(final_response)
            
            # Optional: save to file
            with open("output.md", "w") as f:
                f.write(final_response)
            print("\\nSaved to output.md")
        
        if __name__ == "__main__":
            main()
    """)

def generate_readme(name, desc):
    return dedent(f"""\
        # {name}
        
        {desc}
        
        Generated by Agent Fleet Factory.
        
        ## Usage inside Hermes
        Invoke the skill naturally or via slash command.
        
        ## Standalone
        ```bash
        python scripts/orchestrate.py --task "Your task here"
        ```
        
        ## Customization
        Edit `scripts/orchestrate.py` to change models, add logic, or modify patterns.
    """)

if __name__ == "__main__":
    generate_fleet()
```

### How to Use It Right Now

1. Save the script above as `agent_fleet_generator.py`.
2. Make it executable: `chmod +x agent_fleet_generator.py`
3. Run it: `./agent_fleet_generator.py`
4. Answer the questions (it has good defaults).
5. It creates a full skill folder you can drop into `~/.hermes/skills/`.

**Example workflows you can generate immediately**:
- Code review swarm (security + performance + style + logic specialists → synthesizer)
- AI Output Validator Fleet (exactly like validating the Gemini response: fact-checker, completeness, hallucination/bias, actionability → final validated version)
- Research swarm (your original use case, but now parameterized)
- Any custom pattern

### Making the Generator Itself a Hermes Skill (Conversational Version)

Once you have the generator working, you can turn **it** into a skill:

- Create `skills/meta/agent_fleet_factory/SKILL.md` with a procedure that says:  
  “Describe the multi-agent workflow you want to create…”
- In `scripts/`, include the generator script (or call it via `terminal`).
- When the user invokes the meta-skill, it gathers requirements conversationally, runs the generator logic (via `execute_code` or `terminal python ...`), and then uses Hermes’ `skill_manage` `write_file` / create actions (or simply outputs the files for you to approve) to install the new skill.

This gives you a fully in-Hermes experience: “Create a validator fleet for checking agent outputs” → it asks a few questions → generates and registers the new skill.

### Next-Level Ideas

- Add a `--spec-json` mode so you (or another agent) can feed structured specs programmatically.
- Integrate cost tracking / token logging in the generated `orchestrate.py`.
- Add support for debate patterns (two agents argue, synthesizer judges).
- Let the generator itself use a multi-model approach when creating complex skills.
- Combine with community tools like `hermes-skill-factory` for extra quality scoring.

This system is now **self-reinforcing**: you can use the factory to generate better versions of itself or new specialized factories.

Would you like me to:
- Generate a specific example right now (e.g. the full Code Review Swarm or Validator Fleet package)?
- Refine the generator with more patterns or command-line args?
- Provide the exact SKILL.md + folder structure to make the generator itself a first-class Hermes skill?
- Add features like automatic environment variable handling, output validation, or parallel cost estimation?