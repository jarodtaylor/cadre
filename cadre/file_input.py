"""Caller-layer file reader and composer for ``--doc`` inputs.

Caller-layer only: imported by ``cadre/cli.py`` and
``cadre/data/skill/run.py``. NEVER imported by ``engine.py`` or
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
import stat

from cadre.config import ConfigError

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


def _read_doc(path: str, errors: list[str], truncated: list[str]) -> str | None:
    """Read one ``--doc`` file into a labeled block, or accumulate an error.

    Returns the composed block string on success, or ``None`` after appending a
    path-naming error to ``errors`` (missing / unreadable / non-UTF-8 / non-regular).
    Records ``path`` in ``truncated`` when the file was capped at ``MAX_FILE_BYTES``,
    so the caller can DISCLOSE the truncation on the preview surface. Never raises
    and never returns raw bytes — every failure mode produces a clear error string,
    mirroring ``personas.resolve``.
    """
    # expanduser FIRST — open() does not expand ~ (KTD4). The label/returned path
    # stays as the caller named it; only the open target is expanded.
    target = os.path.expanduser(path)

    # Open with O_NONBLOCK, then fstat the SAME fd and refuse a non-regular file
    # before reading. This is airtight where a stat-then-open-by-path check is not:
    #   - O_NONBLOCK makes open() of a FIFO/device return immediately instead of
    #     blocking forever, so the read-check / run can never hang (R6, KTD7).
    #   - validating + reading the SAME fd closes the stat->open TOCTOU window: a
    #     regular file swapped for a FIFO after the check cannot slip past.
    # KTD4-compatible: no O_NOFOLLOW, so a symlink to a regular file is still
    # followed and read (the cat-equivalent); it constrains the file TYPE, never
    # which path. Mirrors personas.resolve's os.open + os.fdopen idiom.
    fd = None
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        # fstat the RAW fd and reject a non-regular file BEFORE handing it to a reader
        # (os.fdopen on a directory fd would itself raise). This validates the same fd
        # that gets read — no stat->open TOCTOU.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            errors.append(
                f"--doc path is not a regular file (refusing a directory / FIFO / device): {path!r}"
            )
            return None
        # fdopen takes ownership of fd (so the with-block closes it); clear our copy
        # to avoid a double-close in the finally. Read one past the cap so we can
        # detect oversize without slurping a multi-gigabyte file. (O_NONBLOCK has no
        # effect on regular-file reads — they are always ready — so this returns bytes.)
        with os.fdopen(fd, "rb") as fh:
            fd = None
            data = fh.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError) as exc:
        # OSError: missing / permission / I/O. ValueError: embedded NUL in the path
        # (open raises ValueError, not OSError) — must not escape as a traceback
        # (the never-raise contract / KTD5). {path!r} keeps the message escape-safe.
        errors.append(f"--doc file could not be read: {path!r}: {exc}")
        return None
    finally:
        # Close the fd on any path where fdopen never took ownership (open failed →
        # fd is None; a non-regular reject or an fstat error → fd still open).
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

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
        # Note in the model-facing block AND record the path so the caller surfaces
        # the truncation on the human-approval preview (the in-block note alone is
        # invisible there — a reviewer would okay a silently partial file).
        text += _TRUNCATION_NOTE.format(kib=MAX_FILE_BYTES // 1024)
        truncated.append(path)

    return f"{_BLOCK_OPEN.format(path=path)}\n{text}\n{_BLOCK_CLOSE}"


def compose(task: str | None, docs: list[str]) -> tuple[str | None, list[str], list[str]]:
    """Compose ``task`` with the contents of each ``--doc`` file.

    Args:
        task: The base ``--task`` text, or ``None`` when only ``--doc`` was given.
        docs: Ordered list of ``--doc`` paths (as the caller named them).

    Returns:
        ``(composed_task, doc_paths, truncated_paths)``. With no docs the base
        task passes through verbatim and both lists are empty — zero file I/O, so a
        plain task is never regressed (R3). Otherwise the composed task is the base
        text (when present) followed by each file as a labeled block in flag order;
        ``doc_paths`` lists the ``--doc`` paths exactly as the caller named them —
        NOT canonicalized (no realpath; a symlink stays the symlink path) — for the
        preview / read-check, and ``truncated_paths`` is the subset capped at
        ``MAX_FILE_BYTES`` so the
        caller can disclose on the human-approval surface that a review will run over
        a partial file (the in-block note is invisible to the previewer).

    Raises:
        ConfigError: One or more ``--doc`` files could not be read (missing,
            unreadable, non-UTF-8, or non-regular). Every failing path is named;
            errors accumulate into a single raised error (KTD5).
    """
    # No docs: pass the task through untouched, no I/O (R3, AE6).
    if not docs:
        return task, [], []

    errors: list[str] = []
    truncated: list[str] = []
    blocks: list[str] = []
    for path in docs:
        block = _read_doc(path, errors, truncated)
        if block is not None:
            blocks.append(block)

    if errors:
        # A doc-read failure is NOT a fleet-config error — override the header so
        # the operator is pointed at the --doc path, not their fleet YAML (the
        # error-accumulation + single-catch idiom is shared; the framing is not).
        raise ConfigError(errors, header="Could not read --doc input:")

    # Base task (when present) precedes the labeled blocks; a --doc-only run
    # composes from blocks alone (the persona or focus carries the instruction).
    parts = ([task] if task is not None else []) + blocks
    return "\n\n".join(parts), list(docs), truncated
