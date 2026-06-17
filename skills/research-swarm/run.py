#!/usr/bin/env python
"""Hermes skill entry: run the research-swarm fleet on a task.

Thin wrapper — it calls the engine directly (not the CLI) and prints the
rendered result. Invoked by SKILL.md with the task as the argument. Runs on the
Hermes host, where hermes-agent and the user's providers are available.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo root importable when run from the skill directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from fleet_engine.config import ConfigError, FleetConfig  # noqa: E402
from fleet_engine.engine import run_fleet  # noqa: E402
from fleet_engine.model_client import ModelClient  # noqa: E402
from fleet_engine.render import render_result  # noqa: E402

DEFAULT_FLEET = _REPO_ROOT / "fleets" / "research-swarm.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the research-swarm fleet.")
    parser.add_argument("--task", required=True, help="The research task / query")
    parser.add_argument("--fleet", default=str(DEFAULT_FLEET), help="Path to the fleet spec")
    args = parser.parse_args(argv)

    try:
        cfg = FleetConfig.load(args.fleet)
    except ConfigError as err:
        print("Invalid fleet config:\n" + "\n".join(f"  - {m}" for m in err.errors))
        return 1
    except FileNotFoundError:
        print(f"Fleet spec not found: {args.fleet}\n"
              "Copy fleets/research-swarm.example.yaml to fleets/research-swarm.yaml "
              "and set your confirmed provider+model strings.")
        return 1

    result = run_fleet(cfg, args.task, ModelClient())
    print(render_result(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
