"""The configuration schema and its ``invariant.toml`` file format.

The same models validate a configuration file, API payloads and stored rules,
so a rule means the same thing wherever it comes from.
"""

import re
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from invariant.paths import PathError, SourceRoots, normalize_root, validate_path_glob

SCHEMA_VERSION = 1
CONFIG_FILE = "invariant.toml"
PYPROJECT_FILE = "pyproject.toml"

MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
MODULE_GLOB = re.compile(r"^[A-Za-z0-9_.*?\[\]-]+$")
RULE_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

Severity = Literal["error", "warning"]
RULE_TYPES = ("forbidden_import", "no_relative_import", "max_file_lines")


def _module_name(value: str) -> str:
    if not MODULE_NAME.fullmatch(value):
        raise ValueError("must be a dotted Python module name such as 'app.db'")
    return value


def _module_glob(value: str) -> str:
    if not MODULE_GLOB.fullmatch(value):
        raise ValueError("must be a dotted module glob such as 'app.services.*'")
    return value


def _path_glob(value: str) -> str:
    try:
        return validate_path_glob(value)
    except PathError as exc:
        raise ValueError(str(exc)) from exc


def _source_root(value: str) -> str:
    try:
        return normalize_root(value)
    except PathError as exc:
        raise ValueError(str(exc)) from exc


def _rule_key(value: str) -> str:
    if not RULE_KEY.fullmatch(value):
        raise ValueError(
            "must be 1-64 lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return value


def _display_name(value: str) -> str:
    if CONTROL_CHARACTERS.search(value):
        raise ValueError("must not contain control characters")
    return value


ModuleName = Annotated[str, AfterValidator(_module_name)]
ModuleGlob = Annotated[str, Field(max_length=255), AfterValidator(_module_glob)]
PathGlob = Annotated[str, Field(max_length=500), AfterValidator(_path_glob)]
SourceRoot = Annotated[str, Field(max_length=500), AfterValidator(_source_root)]
RuleKey = Annotated[str, AfterValidator(_rule_key)]
DisplayName = Annotated[
    str, Field(min_length=1, max_length=120), AfterValidator(_display_name)
]


class _Model(BaseModel):
    # Unknown fields are mistakes (a misspelt "severty" must not be ignored).
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _RuleBase(_Model):
    key: RuleKey
    name: DisplayName | None = None
    enabled: bool = True
    severity: Severity = "error"
    source_pattern: ModuleGlob = "*"
    exclude_paths: list[PathGlob] = Field(default_factory=list, max_length=100)

    @property
    def display_name(self) -> str:
        return self.name or self.key


class ForbiddenImportRule(_RuleBase):
    """Modules matching ``source_pattern`` must not import these modules."""

    rule_type: Literal["forbidden_import"]
    forbidden_imports: list[ModuleName] = Field(min_length=1, max_length=100)

    @field_validator("forbidden_imports")
    @classmethod
    def unique_modules(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("entries must be unique")
        return value


class NoRelativeImportRule(_RuleBase):
    """Modules matching ``source_pattern`` must use absolute imports only."""

    rule_type: Literal["no_relative_import"]


class MaxFileLinesRule(_RuleBase):
    """Files of matching modules must have at most ``max_lines`` physical lines."""

    rule_type: Literal["max_file_lines"]
    max_lines: Annotated[int, Field(ge=1, le=1_000_000, strict=True)]


RuleConfig = Annotated[
    ForbiddenImportRule | NoRelativeImportRule | MaxFileLinesRule,
    Field(discriminator="rule_type"),
]


class ProjectSettings(_Model):
    source_roots: list[SourceRoot] = Field(
        default_factory=lambda: ["."], min_length=1, max_length=50
    )
    exclude_paths: list[PathGlob] = Field(default_factory=list, max_length=200)
    # Violations of this severity or worse fail the check.
    fail_on: Severity = "error"

    @field_validator("source_roots")
    @classmethod
    def roots_do_not_overlap(cls, value: list[str]) -> list[str]:
        try:
            SourceRoots(value)
        except PathError as exc:
            raise ValueError(str(exc)) from exc
        return value


class InvariantConfig(_Model):
    version: Annotated[int, Field(strict=True)]
    project: ProjectSettings = Field(default_factory=ProjectSettings)
    rules: list[RuleConfig] = Field(default_factory=list, max_length=500)

    @field_validator("version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported configuration version {value}; "
                f"this Invariant reads version {SCHEMA_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def unique_rule_keys(self) -> InvariantConfig:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.key in seen:
                raise ValueError(f"rule key '{rule.key}' is used more than once")
            seen.add(rule.key)
        return self


class ConfigError(Exception):
    """A configuration that cannot be used, with every problem and its location."""

    def __init__(self, source: str, problems: list[tuple[str, str]]) -> None:
        super().__init__(source, problems)
        self.source = source
        self.problems = problems

    def __str__(self) -> str:
        return "\n".join(
            f"{self.source}: {location}: {message}"
            if location
            else f"{self.source}: {message}"
            for location, message in self.problems
        )


def _location(loc: tuple[int | str, ...]) -> str:
    text = ""
    previous: int | str | None = None
    for item in loc:
        if isinstance(item, int):
            text += f"[{item}]"
        elif isinstance(previous, int) and item in RULE_TYPES:
            pass  # the discriminated union's tag, not a field the user wrote
        else:
            text += f".{item}" if text else str(item)
        previous = item
    return text


def validate_config(data: Any, *, source: str = CONFIG_FILE) -> InvariantConfig:
    try:
        return InvariantConfig.model_validate(data)
    except ValidationError as exc:
        problems = [
            (_location(error["loc"]), error["msg"].removeprefix("Value error, "))
            for error in exc.errors()
        ]
        raise ConfigError(source, problems) from exc


def parse_config(
    text: str, *, source: str = CONFIG_FILE, in_pyproject: bool = False
) -> InvariantConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            source, [(f"line {exc.lineno}, column {exc.colno}", exc.msg)]
        ) from exc
    if in_pyproject:
        tool = data.get("tool")
        if not isinstance(tool, dict) or "invariant" not in tool:
            raise ConfigError(source, [("", "no [tool.invariant] table")])
        data = tool["invariant"]
    return validate_config(data, source=source)


def load_config(path: Path) -> InvariantConfig:
    """Load ``invariant.toml``, or the ``[tool.invariant]`` table of a pyproject."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(str(path), [("", f"cannot read the file: {exc}")]) from exc
    return parse_config(
        text, source=str(path), in_pyproject=path.name == PYPROJECT_FILE
    )


def discover_config(directory: Path) -> Path | None:
    """Find the configuration in ``directory``; both candidates at once is an error."""
    config_file = directory / CONFIG_FILE
    pyproject = directory / PYPROJECT_FILE
    in_pyproject = False
    if pyproject.is_file():
        try:
            tool = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool")
        except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
            tool = None  # not our file to judge; it only matters if it is chosen
        in_pyproject = isinstance(tool, dict) and "invariant" in tool
    if config_file.is_file() and in_pyproject:
        raise ConfigError(
            str(directory),
            [
                (
                    "",
                    (
                        f"both {CONFIG_FILE} and [tool.invariant] in "
                        f"{PYPROJECT_FILE} exist; keep one or choose with --config"
                    ),
                )
            ],
        )
    if config_file.is_file():
        return config_file
    return pyproject if in_pyproject else None
