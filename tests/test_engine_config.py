import pytest

from invariant.config import (
    ConfigError,
    ForbiddenImportRule,
    MaxFileLinesRule,
    NoRelativeImportRule,
    discover_config,
    load_config,
    parse_config,
)

FULL_CONFIG = """
version = 1

[project]
source_roots = ["src"]
exclude_paths = ["tests/", "**/migrations/*.py"]
fail_on = "warning"

[[rules]]
key = "domain-isolation"
name = "Domain must not depend on infrastructure"
rule_type = "forbidden_import"
source_pattern = "app.domain.*"
forbidden_imports = ["app.infrastructure", "fastapi"]
exclude_paths = ["src/app/domain/legacy/**"]

[[rules]]
key = "absolute-imports"
rule_type = "no_relative_import"
severity = "warning"

[[rules]]
key = "small-files"
rule_type = "max_file_lines"
max_lines = 400
enabled = false
"""


def problems(text: str) -> list[tuple[str, str]]:
    with pytest.raises(ConfigError) as exc_info:
        parse_config(text)
    return exc_info.value.problems


def test_full_configuration_parses():
    config = parse_config(FULL_CONFIG)

    assert config.project.source_roots == ["src"]
    assert config.project.exclude_paths == ["tests/**", "**/migrations/*.py"]
    assert config.project.fail_on == "warning"
    domain, relative, size = config.rules
    assert isinstance(domain, ForbiddenImportRule)
    assert domain.display_name == "Domain must not depend on infrastructure"
    assert domain.exclude_paths == ["src/app/domain/legacy/**"]
    assert isinstance(relative, NoRelativeImportRule)
    assert (relative.severity, relative.display_name) == ("warning", "absolute-imports")
    assert isinstance(size, MaxFileLinesRule)
    assert (size.max_lines, size.enabled) == (400, False)


def test_defaults_check_the_whole_repository_and_fail_on_errors():
    config = parse_config(
        'version = 1\n[[rules]]\nkey = "r"\nrule_type = "no_relative_import"\n'
    )

    assert config.project.source_roots == ["."]
    assert config.project.exclude_paths == []
    assert config.project.fail_on == "error"
    [rule] = config.rules
    assert (rule.enabled, rule.severity, rule.source_pattern) == (True, "error", "*")


@pytest.mark.parametrize(
    ("snippet", "location", "message"),
    [
        ('severty = "error"', "rules[0].severty", "Extra inputs are not permitted"),
        ("max_lines = 0", "rules[0].max_lines", "greater than or equal to 1"),
        ('max_lines = "5"', "rules[0].max_lines", "valid integer"),
        ("max_lines = true", "rules[0].max_lines", "valid integer"),
        ('severity = "fatal"', "rules[0].severity", "'error' or 'warning'"),
        ('exclude_paths = ["../outside/**"]', "rules[0].exclude_paths[0]", "'..'"),
    ],
)
def test_invalid_rule_fields_report_their_location(snippet, location, message):
    text = f'version = 1\n[[rules]]\nkey = "size"\nrule_type = "max_file_lines"\n{snippet}\n'
    if "max_lines" not in snippet:
        text += "max_lines = 10\n"

    [(found_location, found_message)] = problems(text)

    assert found_location == location
    assert message in found_message


def test_version_is_required_and_checked():
    assert problems("rules = []\n") == [("version", "Field required")]
    [(location, message)] = problems("version = 2\n")
    assert location == "version"
    assert "unsupported configuration version 2" in message
    [(location, _message)] = problems('version = "1"\n')
    assert location == "version"


def test_unknown_rule_type_and_bad_module_are_reported():
    text = 'version = 1\n[[rules]]\nkey = "a"\nrule_type = "forbidden_everything"\n'
    [(location, message)] = problems(text)
    assert location == "rules[0]"
    assert "forbidden_import" in message

    text = (
        'version = 1\n[[rules]]\nkey = "a"\nrule_type = "forbidden_import"\n'
        'forbidden_imports = ["not a module"]\n'
    )
    [(location, message)] = problems(text)
    assert location == "rules[0].forbidden_imports[0]"
    assert "dotted Python module name" in message


def test_rule_keys_must_be_unique_and_well_formed():
    rule = 'rule_type = "no_relative_import"\n'
    duplicate = f'version = 1\n[[rules]]\nkey = "a"\n{rule}[[rules]]\nkey = "a"\n{rule}'
    [(_location, message)] = problems(duplicate)
    assert "rule key 'a' is used more than once" in message

    [(location, _message)] = problems(
        f'version = 1\n[[rules]]\nkey = "Bad Key"\n{rule}'
    )
    assert location == "rules[0].key"


def test_overlapping_source_roots_are_a_configuration_error():
    [(location, message)] = problems(
        'version = 1\n[project]\nsource_roots = ["src", "src/app"]\n'
    )

    assert location == "project.source_roots"
    assert "overlap" in message


def test_toml_syntax_errors_report_line_and_column():
    [(location, _message)] = problems("version = 1\n[[rules]\n")

    assert location.startswith("line 2, column")


def test_error_text_names_the_file_and_field():
    with pytest.raises(ConfigError) as exc_info:
        parse_config("version = 2\n", source="repo/invariant.toml")

    assert str(exc_info.value).startswith("repo/invariant.toml: version: ")


def test_pyproject_tool_table_is_supported(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\n\n[tool.invariant]\nversion = 1\n'
        '[[tool.invariant.rules]]\nkey = "a"\nrule_type = "no_relative_import"\n'
    )

    assert discover_config(tmp_path) == pyproject
    assert [rule.key for rule in load_config(pyproject).rules] == ["a"]


def test_configuration_discovery_refuses_two_sources(tmp_path):
    assert discover_config(tmp_path) is None

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    assert discover_config(tmp_path) is None

    config_file = tmp_path / "invariant.toml"
    config_file.write_text("version = 1\n")
    assert discover_config(tmp_path) == config_file

    (tmp_path / "pyproject.toml").write_text("[tool.invariant]\nversion = 1\n")
    with pytest.raises(ConfigError) as exc_info:
        discover_config(tmp_path)
    assert "choose with --config" in str(exc_info.value)


def test_unreadable_configuration_is_a_configuration_error(tmp_path):
    config_file = tmp_path / "invariant.toml"
    config_file.write_bytes(b"version = 1\n\xff\n")

    with pytest.raises(ConfigError):
        load_config(config_file)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")
