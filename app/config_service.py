"""Export a project's rules and settings as invariant.toml, and import one back."""

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.analysis import engine_rule, project_settings, rule_columns
from app.models import Project, Rule
from invariant.config import (
    SCHEMA_VERSION,
    ConfigError,
    InvariantConfig,
    RuleConfig,
    dump_config,
)

PROJECT_FIELDS = ("source_roots", "exclude_paths", "fail_on")
SOURCE = "invariant.toml"


def export_config(project: Project, rules: list[Rule]) -> str:
    config = InvariantConfig.model_construct(
        version=SCHEMA_VERSION,
        project=project_settings(project),
        rules=[engine_rule(rule) for rule in rules],
    )
    return dump_config(config)


@dataclass
class ImportPlan:
    project_changes: list[tuple[str, Any, Any]] = field(default_factory=list)
    created: list[RuleConfig] = field(default_factory=list)
    updated: list[tuple[Rule, RuleConfig]] = field(default_factory=list)
    deleted: list[Rule] = field(default_factory=list)
    unchanged: list[Rule] = field(default_factory=list)


def plan_import(
    project: Project,
    rules: list[Rule],
    config: InvariantConfig,
    mode: Literal["merge", "replace"],
) -> ImportPlan:
    """Work out what importing ``config`` changes; rules are matched by key."""
    plan = ImportPlan()
    desired = config.project.model_dump()
    for name in PROJECT_FIELDS:
        if getattr(project, name) != desired[name]:
            plan.project_changes.append((name, getattr(project, name), desired[name]))

    existing = {rule.key: rule for rule in rules}
    for rule in config.rules:
        current = existing.get(rule.key)
        if current is None:
            plan.created.append(rule)
        elif _columns(current) != rule_columns(rule):
            plan.updated.append((current, rule))
        else:
            plan.unchanged.append(current)
    imported = {rule.key for rule in config.rules}
    if mode == "replace":
        plan.deleted = [rule for key, rule in existing.items() if key not in imported]

    # Rule names are unique in a project, so check the state after the import.
    owners: dict[str, str] = {}
    kept = (
        []
        if mode == "replace"
        else [r for k, r in existing.items() if k not in imported]
    )
    for rule in kept:
        owners[rule.name] = rule.key
    problems = []
    for index, rule in enumerate(config.rules):
        other = owners.setdefault(rule.display_name, rule.key)
        if other != rule.key:
            problems.append(
                (
                    f"rules[{index}].name",
                    f"the name '{rule.display_name}' is already used by rule '{other}'",
                )
            )
    if problems:
        raise ConfigError(SOURCE, problems)
    return plan


def _columns(rule: Rule) -> dict[str, Any]:
    return {
        "key": rule.key,
        "name": rule.name,
        "rule_type": rule.rule_type,
        "enabled": rule.enabled,
        "severity": rule.severity,
        "source_pattern": rule.source_pattern,
        "exclude_paths": list(rule.exclude_paths),
        "params": dict(rule.params),
    }


def apply_import(db: Session, project: Project, plan: ImportPlan) -> None:
    """Stage every change of ``plan`` in the session; the caller commits once."""
    for name, _before, after in plan.project_changes:
        setattr(project, name, after)
    for rule in plan.deleted:
        db.delete(rule)
    # Give updated rules a name no valid rule can have before renaming them,
    # so two rules can swap names within one import.
    for rule, _config in plan.updated:
        rule.name = f"\x01{rule.id}"
    db.flush()
    for rule, config in plan.updated:
        for column, value in rule_columns(config).items():
            setattr(rule, column, value)
    for config in plan.created:
        db.add(Rule(project_id=project.id, **rule_columns(config)))
