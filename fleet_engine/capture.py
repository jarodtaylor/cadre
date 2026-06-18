"""Capture module — write a run folder with per-specialist markdown and a JSON manifest.

This is a caller-layer module: imported only by cli.py and skills/research-swarm/run.py
(in U3). NEVER imported by engine.py or model_client.py — the engine holds no file I/O.

``save_run`` takes a resolved ``run_dir: Path`` injected by the caller (U3 owns the
fail-fast writability check and env-var resolution). Tests drive ``save_run`` with a
``tempfile.mkdtemp()`` dir and never touch ``~/.cadre``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult

# Default Hermes profile location — recorded in the manifest when HERMES_HOME is unset.
_DEFAULT_HERMES_HOME = "~/.hermes"


def save_run(cfg: FleetConfig, result: FleetResult, run_dir: Path) -> None:
    """Write a complete run folder into ``run_dir``.

    Creates the ``run_dir`` leaf if missing. All artifact files are written
    owner-only (0o600). The synthesizer is represented at run level only
    (synth_ok + synthesis.md), never as a per-lane entry.

    Args:
        cfg: The validated fleet configuration for this run.
        result: The FleetResult returned by run_fleet.
        run_dir: The directory to write artifacts into (injected — caller resolves).
    """
    run_dir.mkdir(parents=True, exist_ok=True)

    # prompt.txt — the task that drove this run.
    _write(run_dir / "prompt.txt", result.task)

    # One markdown file per specialist (success and failure alike).
    for lane in result.specialists:
        filename = f"specialist-{lane.role}.md"
        _write(run_dir / filename, _specialist_md(lane))

    # synthesis.md — the synthesized output, or a failure note.
    _write(run_dir / "synthesis.md", _synthesis_md(result))

    # manifest.json — structured run-health record.
    _write(run_dir / "manifest.json", json.dumps(_build_manifest(cfg, result), indent=2))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` as UTF-8, owner-only (0o600)."""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _specialist_md(lane) -> str:  # lane: AgentResult
    """Markdown file for one specialist lane (success or failure)."""
    lines = [
        f"# Specialist: {lane.role}",
        "",
        f"- **Provider:** {lane.provider}",
        f"- **Model:** {lane.model}",
        f"- **OK:** {lane.ok}",
        f"- **Elapsed:** {lane.elapsed_s:.2f}s" if lane.elapsed_s is not None else "- **Elapsed:** n/a",
        f"- **Toolset:** {lane.toolset if lane.toolset else '(none)'}",
        "",
    ]
    if lane.ok:
        lines += ["## Output", "", lane.text or ""]
    else:
        lines += ["## Error", "", lane.error or "(no error detail)"]
    return "\n".join(lines)


def _synthesis_md(result: FleetResult) -> str:
    """Synthesis markdown: the synthesis text, or an accurate failure note."""
    if result.synthesis is not None:
        return result.synthesis

    # synthesis is None in two cases:
    #   synth_ok is None  → all specialists failed, synthesis never attempted
    #   synth_ok is False → synthesizer ran and failed
    if result.synth_ok is None:
        n_failed = len(result.failures)
        n_total = len(result.specialists)
        return f"No synthesis — {n_failed} of {n_total} specialists failed; synthesis was not attempted."

    # synth_ok is False
    # Pull the synthesizer-failed note from result.notes if present.
    synth_note = next(
        (n for n in result.notes if "synthesizer failed" in n),
        "synthesizer failed",
    )
    return f"No synthesis — {synth_note}."


def _build_manifest(cfg: FleetConfig, result: FleetResult) -> dict:
    """Build the plain-dict manifest; serialized by the caller with json.dumps."""
    participating_models = [
        {"provider": s.provider, "model": s.model}
        for s in result.specialists
    ]

    lanes = []
    for lane in result.specialists:
        lanes.append({
            "role": lane.role,
            "provider": lane.provider,
            "model": lane.model,
            "ok": lane.ok,
            "error": lane.error,
            "elapsed_s": lane.elapsed_s,
            "toolset": list(lane.toolset),  # explicit list — never coerce [] to None
            "timed_out": lane.timed_out,
        })

    return {
        "fleet": result.fleet,
        "task": result.task,
        "timestamp": datetime.now().isoformat(),
        "models": participating_models,
        "synthesizer": {"provider": cfg.synthesis.provider, "model": cfg.synthesis.model},
        "synth_ok": result.synth_ok,
        "hermes_home": os.getenv("HERMES_HOME", _DEFAULT_HERMES_HOME),
        "lanes": lanes,
    }
