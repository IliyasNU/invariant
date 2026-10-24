"""Read the changes of a commit range from Git, never from the working tree.

Revisions are resolved to commit IDs before anything else and are never
handed to a shell or read as options. File contents and the configuration
come from the head commit's objects, so what is checked is exactly that
snapshot, whatever the working tree holds.
"""

import os
import subprocess
from pathlib import Path

from invariant.config import (
    CONFIG_FILE,
    PYPROJECT_FILE,
    ConfigError,
    InvariantConfig,
    choose_config,
    parse_config,
)
from invariant.engine import Unreadable
from invariant.files import MAX_FILE_BYTES, Files, checked_path

GIT_TIMEOUT_SECONDS = 300
_REGULAR_FILE_MODES = ("100644", "100755")
_SYMLINK_MODE = "120000"


class GitError(Exception):
    """Git could not answer: no repository, an unknown revision, no merge base."""


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        run = subprocess.run(
            ["git", "-C", str(root), *args],
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git {args[0]} did not finish within {GIT_TIMEOUT_SECONDS} seconds"
        ) from exc
    if run.returncode:
        message = run.stderr.decode("utf-8", "replace").strip()
        raise GitError(message or f"git {args[0]} failed")
    return run.stdout


def _shallow_hint(root: Path) -> str:
    try:
        shallow = _git(root, "rev-parse", "--is-shallow-repository").strip()
    except GitError:
        return ""
    if shallow != b"true":
        return ""
    return (
        "; the repository is a shallow clone, so fetch the history it needs "
        "(with actions/checkout: fetch-depth: 0)"
    )


def ensure_repository(root: Path) -> None:
    if _git(root, "rev-parse", "--is-inside-work-tree").strip() != b"true":
        raise GitError(f"{root} is not inside a Git work tree")


def resolve_commit(root: Path, revision: str) -> str:
    """The full ID of the commit ``revision`` names."""
    if not revision or revision.startswith("-"):
        raise GitError(f"'{revision}' is not a revision")
    try:
        commit = _git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{revision}^{{commit}}",
        )
    except GitError as exc:
        raise GitError(
            f"'{revision}' does not name a commit{_shallow_hint(root)}"
        ) from exc
    return commit.decode("ascii").strip()


def _show(root: Path, commit: str, name: str) -> bytes | None:
    # "<commit>:./<name>" is relative to root, which may be a subdirectory.
    try:
        return _git(root, "cat-file", "blob", f"{commit}:./{name}")
    except GitError:
        return None  # not in the commit


def committed_config(root: Path, commit: str) -> tuple[InvariantConfig, str] | None:
    """The configuration as committed in ``commit``, and where it was found."""
    config_file = _show(root, commit, CONFIG_FILE)
    pyproject = _show(root, commit, PYPROJECT_FILE)
    where = f"{root} at {commit[:12]}"
    chosen = choose_config(config_file is not None, pyproject, where=where)
    if chosen is None:
        return None
    source = f"{chosen} at {commit[:12]}"
    data = config_file if chosen == CONFIG_FILE else pyproject
    try:
        text = (data or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            source, [("", "cannot read the file: it is not valid UTF-8")]
        ) from exc
    return (
        parse_config(text, source=source, in_pyproject=chosen == PYPROJECT_FILE),
        source,
    )


def _diff_entries(root: Path, base: str, head: str) -> list[tuple[str, str, str, str]]:
    """(status, mode, blob ID, path) of each file changed in head since base."""
    raw = _git(
        root,
        "diff",
        "--raw",
        "-z",
        "--no-abbrev",
        "--find-renames",
        "--relative",
        "--no-color",
        "--no-ext-diff",
        f"{base}...{head}",
    )
    fields = raw.split(b"\0")
    entries = []
    index = 0
    while index < len(fields) and fields[index]:
        # ":<old mode> <new mode> <old blob> <new blob> <status>", then one
        # path, or two (old and new) for a rename or copy.
        _, mode, _, blob, status = fields[index].decode("ascii")[1:].split(" ")
        count = 2 if status[0] in "RC" else 1
        path = os.fsdecode(fields[index + count])
        entries.append((status[0], mode, blob, path))
        index += 1 + count
    return entries


def _blobs(root: Path, ids: list[str]) -> dict[str, bytes | int | None]:
    """Each blob's content, its size if it is too large, or None if missing."""
    found: dict[str, bytes | int | None] = {}
    if not ids:
        return found
    request = "".join(f"{blob}\n" for blob in ids).encode("ascii")
    small = []
    for line in _git(root, "cat-file", "--batch-check", stdin=request).splitlines():
        blob, kind, *size = line.decode("ascii").split(" ")
        if kind != "blob":
            found[blob] = None
        elif int(size[0]) > MAX_FILE_BYTES:
            found[blob] = int(size[0])
        else:
            small.append(blob)
    if small:
        request = "".join(f"{blob}\n" for blob in small).encode("ascii")
        output = _git(root, "cat-file", "--batch", stdin=request)
        position = 0
        for blob in small:
            # "<blob> blob <size>\n<content>\n"
            end = output.index(b"\n", position)
            size = int(output[position:end].split(b" ")[2])
            found[blob] = output[end + 1 : end + 1 + size]
            position = end + 1 + size + 1
    return found


def changed_files(root: Path, base: str, head: str) -> Files:
    """The files ``head`` changed since its merge base with ``base``.

    Removed and non-Python files are only counted; renamed files are checked
    under their new path.
    """
    try:
        entries = _diff_entries(root, base, head)
    except GitError as exc:
        raise GitError(f"{exc}{_shallow_hint(root)}") from exc
    files = Files()
    wanted: dict[str, list[str]] = {}
    for status, mode, blob, name in entries:
        if status == "D" or not name.endswith(".py"):
            files.ignored += 1
            continue
        path, problem = checked_path(name)
        if problem:
            files.unreadable.append(Unreadable(path, "invalid_path", problem))
        elif mode == _SYMLINK_MODE:
            files.unreadable.append(Unreadable(path, "symlink"))
        elif mode not in _REGULAR_FILE_MODES:
            files.ignored += 1  # a submodule
        else:
            wanted.setdefault(blob, []).append(path)
    for blob, content in _blobs(root, list(wanted)).items():
        for path in wanted[blob]:
            if isinstance(content, bytes):
                files.add(path, content)
            elif content is None:
                files.unreadable.append(
                    Unreadable(path, "unreadable", "the content is missing from Git")
                )
            else:
                files.unreadable.append(Unreadable(path, "too_large"))
    return files
