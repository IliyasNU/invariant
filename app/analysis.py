import ast
from dataclasses import dataclass
from fnmatch import fnmatchcase

from app.models import Rule
from app.schemas import SourceFile, ViolationResponse


@dataclass(frozen=True)
class SourceSyntaxError(Exception):
    path: str
    line: int | None
    column: int | None
    message: str


def _module_name(path: str) -> str:
    module = path.removesuffix(".py").replace("/", ".")
    if module.endswith(".__init__"):
        module = module.removesuffix(".__init__")
    return module


def _imports(tree: ast.AST) -> list[tuple[str, int, int]]:
    imported: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(
                (alias.name, node.lineno, node.col_offset + 1) for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imported.append((module, node.lineno, node.col_offset + 1))
    return imported


def analyze_sources(
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

        module = _module_name(source.path)
        imports = _imports(tree)
        for rule in rules:
            if rule.rule_type != "forbidden_import" or not fnmatchcase(
                module, rule.source_pattern
            ):
                continue

            for imported_module, line, column in imports:
                for forbidden in rule.forbidden_imports:
                    if imported_module == forbidden or imported_module.startswith(
                        f"{forbidden}."
                    ):
                        violations.append(
                            ViolationResponse(
                                rule_id=rule.id,
                                rule_name=rule.name,
                                path=source.path,
                                line=line,
                                column=column,
                                imported_module=imported_module,
                                message=(
                                    f"{module} must not import {forbidden} "
                                    f"(matched {imported_module})"
                                ),
                            )
                        )
                        break

    return sorted(
        violations,
        key=lambda item: (item.path, item.line, item.column, item.rule_id),
    )
