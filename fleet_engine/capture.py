"""Capture module — write a run folder with per-specialist markdown and a JSON manifest.

This is a caller-layer module: imported only by cli.py and skills/research-swarm/run.py
(in U3). NEVER imported by engine.py or model_client.py — the engine holds no file I/O.

``save_run`` takes a resolved ``run_dir: Path`` injected by the caller (U3 owns the
fail-fast writability check and env-var resolution). Tests drive ``save_run`` with a
``tempfile.mkdtemp()`` dir and never touch ``~/.cadre``.

``resolve_run_dir(task)`` is a PURE path resolver: returns ``CADRE_RUN_DIR``
(expanduser) when that env var is set, else ``~/.cadre/runs/<YYYY-MM-DD-HHMMSS>-<slug>``.
No filesystem access — just computes a Path.

``prepare_run_dir(task, run_dir=None)`` resolves, atomically reserves/creates, and probes
writability — raising OSError on any unrecoverable failure. Callers use this instead of
inline mkdir, then fail-fast on the OSError before calling run_fleet.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult

# Default Hermes profile location — recorded in the manifest when HERMES_HOME is unset.
_DEFAULT_HERMES_HOME = "~/.hermes"

# Default root for run folders when CADRE_RUN_DIR is not set.
_DEFAULT_RUNS_ROOT = "~/.cadre/runs"

# Max length of the task-derived slug in a default run-dir leaf. Truncation
# cuts on a word boundary (see _slugify), so a long task never ends mid-word.
_SLUG_MAX = 40


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
    lowercase, has no leading/trailing dashes, and is bounded to ``_SLUG_MAX``
    characters.  A task that reduces to an empty string falls back to ``"run"``.

    Truncation cuts on a **word boundary**: an over-long slug is trimmed back to
    the last whole hyphen-separated word within the limit, so a long task yields
    ``...-agent-pattern`` rather than a sliced ``...-inspi``.  A single word
    longer than the limit (no boundary to cut on) is hard-cut.

    Security: the explicit ASCII whitelist ``[^a-z0-9]+`` (not ``\\W``) ensures
    no non-ASCII letters pass through, and the result can never contain '/' or
    '.' — so it cannot escape the runs directory when used as a path component.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
    if len(slug) > _SLUG_MAX:
        head = slug[:_SLUG_MAX]
        boundary = head.rfind("-")
        # Cut at the last whole word; hard-cut only if the first word alone
        # already exceeds the limit (no usable boundary in range).
        slug = head[:boundary] if boundary > 0 else head
    return slug or "run"


def resolve_run_dir(task: str) -> Path:
    """Return the run directory Path for this task — PURE resolver, no FS access.

    If ``CADRE_RUN_DIR`` is set, use it verbatim (expanduser only — no stamp or
    slug leaf is appended; the caller controls the full path).

    Otherwise, build ``~/.cadre/runs/<YYYY-MM-DD-HHMMSS>-<slug>`` from the
    current time and a sanitized slug of the task.  The returned Path is NOT
    created — call ``prepare_run_dir`` to atomically reserve and create it.
    """
    cadre_run_dir = os.getenv("CADRE_RUN_DIR")
    if cadre_run_dir:
        return Path(cadre_run_dir).expanduser()

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    slug = _slugify(task)
    runs_root = Path(_DEFAULT_RUNS_ROOT).expanduser()
    return runs_root / f"{stamp}-{slug}"


def prepare_run_dir(task: str, run_dir: Path | None = None) -> Path:
    """Resolve, atomically create, and probe the run directory; return its Path.

    This is the single place that touches the filesystem for directory creation.
    Callers should call this BEFORE ``run_fleet`` so a bad path fails fast with
    no model calls wasted.

    Two modes:

    **Default path** (``run_dir`` is None AND ``CADRE_RUN_DIR`` is unset):
        Atomically reserves the directory by attempting ``mkdir(exist_ok=False)``.
        On ``FileExistsError`` (same-second collision), appends ``-2``, ``-3``, …
        until a unique leaf is created. The process that creates the directory is
        the sole owner — no TOCTOU race.

    **Explicit path** (``run_dir`` injected, OR ``CADRE_RUN_DIR`` is set):
        Creates with ``mkdir(parents=True, exist_ok=True)`` — reuse-by-design;
        the caller controls the full path.

    In both modes:
    - All mkdir calls run under a tightened ``umask(0o077)`` so every created
      directory component (including parents) is owner-only (0o700).
    - After creation, a writability probe creates and deletes a sentinel file.
      If that raises OSError, it is re-raised so the caller fails fast BEFORE
      any model calls are made.

    Args:
        task: The task string; used to derive the default leaf name.
        run_dir: Optional explicit path; overrides the default resolution.

    Returns:
        The Path of the created (or pre-existing explicit) run directory.

    Raises:
        OSError: If the directory cannot be created or is not writable.
    """
    # Decide which mode: explicit means the caller injected run_dir, OR
    # CADRE_RUN_DIR is set (resolve_run_dir will return it verbatim).
    cadre_run_dir = os.getenv("CADRE_RUN_DIR")
    explicit = (run_dir is not None) or bool(cadre_run_dir)

    if run_dir is None:
        run_dir = resolve_run_dir(task)

    old_umask = os.umask(0o077)
    try:
        if explicit:
            # Reuse-by-design: user controls the path.
            run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            # Atomic reservation: loop until we own a fresh leaf.
            base = run_dir
            counter = 2
            while True:
                try:
                    run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                    break
                except FileExistsError:
                    run_dir = base.parent / f"{base.name}-{counter}"
                    counter += 1

        # Writability probe — fail fast before wasting model calls. Uses a
        # unique temp name (not a fixed sentinel) so two processes sharing an
        # explicit run_dir can't race on the probe's create/unlink.
        with tempfile.NamedTemporaryFile(
            dir=run_dir, prefix=".cadre-write-test-", delete=True
        ):
            pass
    finally:
        os.umask(old_umask)

    return run_dir


def save_run(cfg: FleetConfig, result: FleetResult, run_dir: Path) -> None:
    """Write a complete run folder into ``run_dir``.

    Creates the ``run_dir`` leaf if missing, owner-only (0o700). All artifact
    files are written owner-only (0o600). The synthesizer is represented at run
    level only (synth_ok + synthesis.md), never as a per-lane entry.

    ``manifest.json`` is written LAST intentionally — it serves as a
    run-completion marker.  A reader can treat its absence as a partial or
    failed write.

    Specialist filenames are deduplicated after ``_safe_role`` sanitization:
    if two distinct roles reduce to the same safe name (e.g. 'a/b' and 'a:b'
    both → 'a-b'), the second gets '-2', the third '-3', etc.  The actual
    written filename is recorded in the manifest lane under the ``"file"`` key.

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
    # Track used filenames (stem only) to disambiguate sanitization collisions.
    used_stems: set[str] = set()
    lane_filenames: list[str] = []
    for lane in result.specialists:
        stem = f"specialist-{_safe_role(lane.role)}"
        if stem not in used_stems:
            used_stems.add(stem)
            filename = f"{stem}.md"
        else:
            counter = 2
            while f"{stem}-{counter}" in used_stems:
                counter += 1
            stem = f"{stem}-{counter}"
            used_stems.add(stem)
            filename = f"{stem}.md"
        lane_filenames.append(filename)
        _write(run_dir / filename, _specialist_md(lane))

    # synthesis.md — the synthesized output, or a failure note.
    _write(run_dir / "synthesis.md", _synthesis_md(result))

    # manifest.json — structured run-health record.
    _write(
        run_dir / "manifest.json",
        json.dumps(_build_manifest(cfg, result, lane_filenames), indent=2),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` as UTF-8, owner-only (0o600).

    Opens with mode 0o600 so the file is owner-only *at creation* — never the
    momentary 0o644 a write-then-chmod leaves under a default umask. This bites
    in the explicit-dir case (e.g. ``CADRE_RUN_DIR`` pointing at a pre-existing
    world-traversable directory); the default ``~/.cadre`` chain is already
    0o700. The trailing chmod still tightens a pre-existing file, since O_CREAT
    does not alter the mode of a file that already exists (re-run into a dir).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
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


def _build_manifest(cfg: FleetConfig, result: FleetResult, lane_filenames: list[str]) -> dict:
    """Build the plain-dict manifest; serialized by the caller with json.dumps.

    ``lane_filenames`` is the list of actual on-disk filenames (one per specialist,
    in the same order as ``result.specialists``) so the manifest is an accurate
    index to the files — even when sanitization caused two roles to share a base
    name and one was renamed to ``...-2.md``.
    """
    participating_models = [
        {"provider": s.provider, "model": s.model}
        for s in result.specialists
    ]

    lanes = []
    for lane, filename in zip(result.specialists, lane_filenames):
        lanes.append({
            "role": lane.role,
            "provider": lane.provider,
            "model": lane.model,
            "ok": lane.ok,
            "error": lane.error,
            "elapsed_s": lane.elapsed_s,
            "toolset": list(lane.toolset),  # explicit list — never coerce [] to None
            "timed_out": lane.timed_out,
            "file": filename,
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
