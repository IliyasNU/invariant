import ast
from dataclasses import dataclass
from fnmatch import fnmatchcase

from app.models import Rule
from app.schemas import SourceFile, ViolationResponse


@dataclass
class SourceSyntaxError(Exception):
    path: str
    line: int | None
    column: int | None
    message: str


@dataclass(frozen=True)
class _Import:
    module: str
    line: int
    column: int
    # Names bound by ``from module import name``. Any of them may be a
    # submodule, so each is also checked as ``module.name``.
    names: tuple[str, ...] = ()


def _module_name(path: str) -> str:
    module = path.removesuffix(".py").replace("/", ".")
    if module.endswith(".__init__"):
        module = module.removesuffix(".__init__")
    return module


def _absolute_module(package: list[str], level: int, module: str | None) -> str | None:
    """Resolve ``from <level dots><module> import ...`` like Python's importer.

    Returns None for a relative import that climbs above the top-level package.
    """
    if level == 0:
        return module
    keep = len(package) - (level - 1)
    if keep < 1:
        return None
    return ".".join([*package[:keep], *([module] if module else [])])


def _imports(tree: ast.AST, package: list[str]) -> list[_Import]:
    imported: list[_Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(
                _Import(alias.name, node.lineno, node.col_offset + 1)
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(package, node.level, node.module)
            if module is None:
                module = "." * node.level + (node.module or "")
            names = tuple(alias.name for alias in node.names if alias.name != "*")
            imported.append(_Import(module, node.lineno, node.col_offset + 1, names))
    return imported


def _rule_applies(module: str, source_pattern: str) -> bool:
    # "app.domain.*" also covers the package itself (app/domain/__init__.py).
    return fnmatchcase(module, source_pattern) or (
        source_pattern.endswith(".*") and fnmatchcase(module, source_pattern[:-2])
    )


def _forbidden_match(imported_module: str, forbidden_imports: list[str]) -> str | None:
    for forbidden in forbidden_imports:
        if imported_module == forbidden or imported_module.startswith(f"{forbidden}."):
            return forbidden
    return None


def analyze_files(
    files: list[SourceFile], rules: list[Rule]
) -> list[ViolationResponse]:
    violations: list[ViolationResponse] = []

    for source in files:
        try:
            tree = ast.parse(source.content, filename=source.path)
        except SyntaxError as exc:
            raise SourceSyntaxError(
                path=source.path,
                line=exc.lineno,
                column=exc.offset,
                message=exc.msg,
            ) from exc
        except (MemoryError, RecursionError) as exc:
            # CPython reports deeply nested source this way instead of SyntaxError.
            raise SourceSyntaxError(
                path=source.path,
                line=None,
                column=None,
                message="Python source is too complex to parse",
            ) from exc

        module = _module_name(source.path)
        # Relative imports resolve against the file's directory, for both
        # plain modules and package __init__ files.
        imports = _imports(tree, package=source.path.split("/")[:-1])
        for rule in rules:
            if rule.rule_type != "forbidden_import" or not _rule_applies(
                module, rule.source_pattern
            ):
                continue

            for imported in imports:
                candidates = [imported.module]
                if _forbidden_match(imported.module, rule.forbidden_imports) is None:
                    candidates = [
                        f"{imported.module}.{name}" for name in imported.names
                    ]
                for imported_module in candidates:
                    forbidden = _forbidden_match(
                        imported_module, rule.forbidden_imports
                    )
                    if forbidden is None:
                        continue
                    violations.append(
                        ViolationResponse(
                            rule_id=rule.id,
                            rule_name=rule.name,
                            path=source.path,
                            line=imported.line,
                            column=imported.column,
                            imported_module=imported_module,
                            message=(
                                f"{module} must not import {forbidden} "
                                f"(matched {imported_module})"
                            ),
                        )
                    )

    return sorted(
        violations,
        key=lambda item: (item.path, item.line, item.column, item.rule_id),
    )
