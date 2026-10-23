"""Repository-relative paths, path globs and source roots."""

import glob
import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DRIVE = re.compile(r"^[A-Za-z]:")


class PathError(ValueError):
    """A path that cannot name a file or directory inside the repository."""


def _normalize(path: str) -> str:
    if not path:
        raise PathError("path is empty")
    if _CONTROL_CHARACTERS.search(path):
        raise PathError("path contains control characters")
    # Backslashes are separators, so Windows paths match GitHub's POSIX paths.
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE.match(normalized):
        raise PathError("path must be relative to the repository root")
    if ".." in normalized.split("/"):
        raise PathError("path must not contain '..'")
    return posixpath.normpath(normalized)


def normalize_path(path: str) -> str:
    """Return ``path`` as a canonical repository-relative POSIX path to a file."""
    normalized = _normalize(path)
    if normalized == ".":
        raise PathError("path names the repository root, not a file")
    return normalized


def normalize_root(root: str) -> str:
    """Return a source root as a canonical directory path; ``.`` is the root."""
    return _normalize(root)


def validate_path_glob(pattern: str) -> str:
    """Normalize a repository-relative path glob such as ``tests/**``.

    ``*`` and ``?`` stay inside one path segment and ``**`` spans directories.
    A trailing slash names a whole directory: ``tests/`` means ``tests/**``.
    """
    directory = pattern.endswith(("/", "\\"))
    normalized = _normalize(pattern.rstrip("/\\") or ".")
    if normalized == ".":
        raise PathError("pattern must not name the repository root")
    if directory and not (normalized == "**" or normalized.endswith("/**")):
        normalized += "/**"
    try:
        _glob_regex(normalized)
    except re.error as exc:
        raise PathError(f"invalid pattern: {exc}") from exc
    return normalized


@lru_cache(maxsize=4096)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(
        glob.translate(pattern, recursive=True, include_hidden=True, seps="/")
    )


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Whether a normalized path matches any normalized path glob."""
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


@dataclass(frozen=True)
class ModuleLocation:
    module: str
    # The package that relative imports in the file resolve against.
    package: tuple[str, ...]


class SourceRoots:
    """Directories whose contents map to importable module names.

    ``src/app/domain/a.py`` is the module ``app.domain.a`` under the root
    ``src`` and ``src.app.domain.a`` under the default root ``.``.
    """

    def __init__(self, roots: Sequence[str] = (".",)) -> None:
        normalized = [normalize_root(root) for root in roots]
        if not normalized:
            raise PathError("at least one source root is required")
        for index, root in enumerate(normalized):
            for other in normalized[:index]:
                if root == other:
                    raise PathError(f"source root '{root}' is listed twice")
                if "." in (root, other) or (
                    root.startswith(other + "/") or other.startswith(root + "/")
                ):
                    raise PathError(
                        f"source roots '{other}' and '{root}' overlap; "
                        "a file must belong to exactly one root"
                    )
        self.roots = tuple(normalized)

    def locate(self, path: str) -> ModuleLocation | None:
        """Map a normalized file path to its module, or None outside every root."""
        for root in self.roots:
            if root == ".":
                relative = path
            elif path.startswith(root + "/"):
                relative = path[len(root) + 1 :]
            else:
                continue
            parts = relative.split("/")
            module = relative.removesuffix(".py").replace("/", ".")
            if module.endswith(".__init__"):
                module = module.removesuffix(".__init__")
            # For a module and for a package's __init__.py alike, relative
            # imports resolve against the directory that holds the file.
            return ModuleLocation(module=module, package=tuple(parts[:-1]))
        return None


def absolute_module(
    package: tuple[str, ...], level: int, module: str | None
) -> str | None:
    """Resolve ``from <level dots><module> import ...`` as Python's importer does.

    Returns None for a relative import that climbs above the top-level package.
    """
    if level == 0:
        return module
    keep = len(package) - (level - 1)
    if keep < 1:
        return None
    return ".".join([*package[:keep], *([module] if module else [])])
