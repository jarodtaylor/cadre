"""Caller-layer file reader and composer for ``--doc`` inputs.

Caller-layer only: imported by ``fleet_engine/cli.py`` and
``skills/cadre-fleet/run.py``. NEVER imported by ``engine.py`` or
``model_client.py`` — the engine receives only the finished task *string* and
gains no file I/O (the import-isolation guard in ``tests/test_personas.py``
``TestEngineIsolation`` enforces this; pattern:
``docs/solutions/architecture-patterns/side-effects-at-the-edge-pure-engine-core.md``).

``compose`` reads each named ``--doc`` file and appends its text to the task as a
delimited, labeled block, in the order the flags were given. It mirrors
``personas.resolve``'s error-accumulation — collect every per-file error and raise
ONE ``ConfigError`` — so both callers handle a bad ``--doc`` through the same
``except ConfigError`` clause that already guards ``load`` + ``resolve`` (KTD5),
exiting cleanly instead of tracebacking.

Trust model (KTD4): the helper opens exactly the file the caller named — no
allowlist, traversal rejection, or root jail. A ``--doc`` path is a thing the
operator (or the agent, with the human's preview okay) explicitly chose to read,
the ``cat``-equivalent relocated into the runner. It MUST still ``expanduser``
before ``open`` — ``open("~/x")`` does not expand ``~`` (the silent-failure
footgun; ``docs/solutions/design-patterns/expanduser-before-realpath-for-confined-paths.md``).

Sanitize scope (KTD6): the injected file *content* is returned verbatim — NOT
sanitized — because stripping control bytes would corrupt the very document under
review. Output-side hardening of model/file text on the render/capture path is the
deferred #5 / #23 surface. The preview's path *labels* are sanitized separately by
``render.render_file_inputs``.
"""

from __future__ import annotations

import codecs
import os

from fleet_engine.config import ConfigError

# Cap on bytes injected per ``--doc`` file. A reviewer's plan/diff is well under
# this; the cap exists so a stray huge or binary path cannot inject an unbounded
# blob into the task. Tunable — raise it if a real document trips it.
MAX_FILE_BYTES = 256 * 1024  # 256 KiB

# Block delimiters. The label carries the source path so a multi-doc task stays
# attributable (R2). This is model-facing task text, not a terminal surface, so it
# is NOT sanitized (KTD6) — the preview render layer sanitizes the path labels it
# shows to the human.
_BLOCK_OPEN = "=== FILE: {path} ==="
_BLOCK_CLOSE = "=== END FILE ==="
_TRUNCATION_NOTE = (
    "\n\n[cadre: this file exceeded {kib} KiB and was truncated; "
    "the remainder was omitted]"
)


def _read_doc(path: str, errors: list[str]) -> str | None:
    """Read one ``--doc`` file into a labeled block, or accumulate an error.

    Returns the composed block string on success, or ``None`` after appending a
    path-naming error to ``errors`` (missing / unreadable / non-UTF-8). Never
    raises and never returns raw bytes — every failure mode produces a clear
    error string, mirroring ``personas.resolve``.
    """
    # expanduser FIRST — open() does not expand ~ (KTD4). The label/returned path
    # stays as the caller named it; only the open target is expanded.
    target = os.path.expanduser(path)
    try:
        with open(target, "rb") as fh:
            # Read one past the cap so we can detect oversize without slurping a
            # multi-gigabyte file into memory.
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        errors.append(f"--doc file could not be read: {path!r}: {exc}")
        return None

    oversize = len(data) > MAX_FILE_BYTES
    if oversize:
        data = data[:MAX_FILE_BYTES]

    # Decode strictly so a non-UTF-8 / binary file is a loud error (R6 / AE5), with
    # one nuance for the oversize case: the byte cap can split a trailing multibyte
    # character. An incremental decoder with final=False buffers (drops) only a
    # valid-so-far partial tail — a genuinely invalid byte anywhere still raises —
    # so a truncated text file decodes cleanly while a binary file still errors.
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        text = decoder.decode(data, final=not oversize)
    except UnicodeDecodeError:
        errors.append(f"--doc file is not valid UTF-8 text: {path!r}")
        return None

    if oversize:
        text += _TRUNCATION_NOTE.format(kib=MAX_FILE_BYTES // 1024)

    return f"{_BLOCK_OPEN.format(path=path)}\n{text}\n{_BLOCK_CLOSE}"


def compose(task: str | None, docs: list[str]) -> tuple[str | None, list[str]]:
    """Compose ``task`` with the contents of each ``--doc`` file.

    Args:
        task: The base ``--task`` text, or ``None`` when only ``--doc`` was given.
        docs: Ordered list of ``--doc`` paths (as the caller named them).

    Returns:
        ``(composed_task, resolved_paths)``. With no docs the base task passes
        through verbatim and ``resolved_paths`` is empty — zero file I/O, so a
        plain task is never regressed (R3). Otherwise the composed task is the
        base text (when present) followed by each file as a labeled block in flag
        order, and ``resolved_paths`` lists the doc paths as named (for the
        preview / read-check).

    Raises:
        ConfigError: One or more ``--doc`` files could not be read (missing,
            unreadable, or non-UTF-8). Every failing path is named; errors
            accumulate into a single raised error (KTD5).
    """
    # No docs: pass the task through untouched, no I/O (R3, AE6).
    if not docs:
        return task, []

    errors: list[str] = []
    blocks: list[str] = []
    for path in docs:
        block = _read_doc(path, errors)
        if block is not None:
            blocks.append(block)

    if errors:
        raise ConfigError(errors)

    # Base task (when present) precedes the labeled blocks; a --doc-only run
    # composes from blocks alone (the persona or focus carries the instruction).
    parts = ([task] if task is not None else []) + blocks
    return "\n\n".join(parts), list(docs)
