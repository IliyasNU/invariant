"""Read the files to analyze, with the same rules for every caller.

A Python file is analyzed when it is a regular file of at most
``MAX_FILE_BYTES`` that decodes as UTF-8 (a byte order mark is allowed).
Anything else is reported as not analyzed rather than guessed at, so the
result says it is incomplete instead of passing without it.
"""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from invariant.engine import Source, Unreadable, UnreadableReason
from invariant.paths import PathError, normalize_path

MAX_FILE_BYTES = 1_000_000

# Directories that hold no project code. Hidden directories (``.git``,
# ``.venv``, ``.tox``) and virtual environments of any name are skipped too.
SKIPPED_DIRECTORIES = frozenset({"__pycache__", "node_modules"})


def decode_source(data: bytes) -> str:
    """Python source as text; raises UnicodeDecodeError unless it is UTF-8."""
    return data.decode("utf-8-sig")


@dataclass
class Files:
    """The files of one analysis, ready for :func:`invariant.engine.analyze`."""

    sources: list[Source] = field(default_factory=list)
    unreadable: list[Unreadable] = field(default_factory=list)
    # Removed and non-Python files.
    ignored: int = 0

    def add(self, path: str, data: bytes) -> None:
        """Add a Python file's content, or the reason it cannot be analyzed."""
        if len(data) > MAX_FILE_BYTES:
            self.unreadable.append(Unreadable(path, "too_large"))
            return
        try:
            self.sources.append(Source(path, decode_source(data)))
        except UnicodeDecodeError:
            self.unreadable.append(Unreadable(path, "undecodable"))


def checked_path(path: str) -> tuple[str, str | None]:
    """A path fit for output, and why it cannot be analyzed, if it cannot.

    File names that are not valid UTF-8 or contain a backslash (a separator
    for Invariant, a plain character on POSIX) cannot be told apart from other
    paths, so they are reported instead of being analyzed under another name.
    """
    shown = path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    if shown != path:
        return shown, "the file name is not valid UTF-8"
    if os.sep == "/" and "\\" in path:
        return shown, "the file name contains a backslash"
    try:
        normalize_path(path)
    except PathError as exc:
        return shown, str(exc)
    return shown, None


def _skipped(directory: Path) -> bool:
    return (
        directory.name.startswith(".")
        or directory.name in SKIPPED_DIRECTORIES
        or (directory / "pyvenv.cfg").is_file()
    )


def _read(full: Path) -> bytes:
    # O_NOFOLLOW: a file swapped for a link after it was listed is not followed.
    # O_NONBLOCK: a FIFO named x.py fails the regular-file check below instead
    # of blocking until something writes to it.
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(full, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        # One byte more than the limit is enough to know the file is too large.
        return handle.read(MAX_FILE_BYTES + 1)


def collect_directory(root: Path) -> Files:
    """Every file below ``root``, with Python files read from disk.

    Symbolic links are never followed: a linked directory is not entered and
    a linked Python file is reported as not analyzed.
    """
    files = Files()

    def walk_error(error: OSError) -> None:
        path, _ = checked_path(Path(error.filename).relative_to(root).as_posix())
        files.unreadable.append(Unreadable(path, "unreadable", error.strerror))

    for directory, subdirectories, names in os.walk(root, onerror=walk_error):
        here = Path(directory)
        subdirectories[:] = sorted(
            name for name in subdirectories if not _skipped(here / name)
        )
        for name in sorted(names):
            full = here / name
            if not name.endswith(".py"):
                files.ignored += 1
                continue
            path, problem = checked_path(full.relative_to(root).as_posix())
            reason: UnreadableReason
            if problem:
                reason, detail = "invalid_path", problem
            elif full.is_symlink():
                reason, detail = "symlink", None
            else:
                try:
                    data = _read(full)
                except OSError as exc:
                    reason, detail = "unreadable", exc.strerror or str(exc)
                else:
                    files.add(path, data)
                    continue
            files.unreadable.append(Unreadable(path, reason, detail))
    return files
