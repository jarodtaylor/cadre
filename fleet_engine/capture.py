"""Capture module — write a run folder with per-specialist markdown and a JSON manifest.

This is a caller-layer module: imported only by cli.py and skills/research-swarm/run.py
(in U3). NEVER imported by engine.py or model_client.py — the engine holds no file I/O.

``save_run`` takes a resolved ``run_dir: Path`` injected by the caller (U3 owns the
fail-fast writability check and env-var resolution). Tests drive ``save_run`` with a
``tempfile.mkdtemp()`` dir and never touch ``~/.cadre``.

``resolve_run_dir(task)`` is the caller-layer resolver: returns ``CADRE_RUN_DIR``
(expanduser) when that env var is set, else ``~/.cadre/runs/<YYYY-MM-DD-HHMMSS>-<slug>``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult

# Default Hermes profile location — recorded in the manifest when HERMES_HOME is unset.
_DEFAULT_HERMES_HOME = "~/.hermes"

# Default root for run folders when CADRE_RUN_DIR is not set.
_DEFAULT_RUNS_ROOT = "~/.cadre/runs"


def _safe_role(role: str) -> str:
    """Return a filesystem-safe version of ``role`` for use in filenames ONLY.

    Replaces every character outside ``[A-Za-z0-9_-]`` with '-'.  Case is
    preserved (role-uniqueness is case-sensitive; lowercasing would collide
    'Web' and 'web').  Falls back to 'unknown' if the sanitized name is empty.

    The manifest's ``role`` field and the markdown header always use the TRUE
    (un-sanitized) role — this helper is called only where a filename is built.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", role)
    return sanitized or "unknown"


def _slugify(task: str) -> str:
    """Return a filesystem-safe slug derived from ``task``.

    Whitelist: ``[a-z0-9-]``.  All other characters (including path separators,
    '..', spaces, and control characters) are replaced with '-'.  The result is
    lowercase, has no leading/trailing dashes, and is bounded to 40 characters.
    A task that reduces to an empty string falls back to ``"run"``.

    Security: the explicit ASCII whitelist ``[^a-z0-9]+`` (not ``\\W``) ensures
    no non-ASCII letters pass through, and the result can never contain '/' or
    '.' — so it cannot escape the runs directory when used as a path component.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())
    slug = slug.strip("-")
    slug = slug[:40].rstrip("-")  # truncation can leave a trailing dash
    return slug or "run"


def resolve_run_dir(task: str) -> Path:
    """Return the run directory for this task.

    If ``CADRE_RUN_DIR`` is set, use it verbatim (expanduser only — no stamp or
    slug leaf is appended; the caller controls the full path).

    Otherwise, build ``~/.cadre/runs/<YYYY-MM-DD-HHMMSS>-<slug>`` from the
    current time and a sanitized slug of the task.
    """
    cadre_run_dir = os.getenv("CADRE_RUN_DIR")
    if cadre_run_dir:
        return Path(cadre_run_dir).expanduser()

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    slug = _slugify(task)
    leaf = f"{stamp}-{slug}"
    runs_root = Path(_DEFAULT_RUNS_ROOT).expanduser()
    candidate = runs_root / leaf
    if not candidate.exists():
        return candidate
    # Collision: two runs in the same second — append -2, -3, … until unused.
    counter = 2
    while True:
        candidate = runs_root / f"{leaf}-{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


def save_run(cfg: FleetConfig, result: FleetResult, run_dir: Path) -> None:
    """Write a complete run folder into ``run_dir``.

    Creates the ``run_dir`` leaf if missing, owner-only (0o700). All artifact
    files are written owner-only (0o600). The synthesizer is represented at run
    level only (synth_ok + synthesis.md), never as a per-lane entry.

    ``manifest.json`` is written LAST intentionally — it serves as a
    run-completion marker.  A reader can treat its absence as a partial or
    failed write.

    Args:
        cfg: The validated fleet configuration for this run.
        result: The FleetResult returned by run_fleet.
        run_dir: The directory to write artifacts into (injected — caller resolves).
    """
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # prompt.txt — the task that drove this run.
    _write(run_dir / "prompt.txt", result.task)

    # One markdown file per specialist (success and failure alike).
    # _safe_role sanitizes the role for the FILENAME ONLY; the markdown content
    # and manifest always use the true (un-sanitized) lane.role.
    for lane in result.specialists:
        filename = f"specialist-{_safe_role(lane.role)}.md"
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
