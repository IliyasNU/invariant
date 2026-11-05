import json

import pytest

from invariant import __version__
from invariant.cli import main
from invariant.config import ForbiddenImportRule, MaxFileLinesRule
from invariant.engine import Source, Unreadable, analyze
from invariant.sarif import to_sarif


def import_rule(**fields):
    return ForbiddenImportRule(
        key="no-os", rule_type="forbidden_import", forbidden_imports=["os"], **fields
    )


def test_sarif_uses_stable_rule_ids_metadata_and_unicode_locations():
    rules = [
        MaxFileLinesRule(key="z-size", rule_type="max_file_lines", max_lines=50),
        import_rule(name="No operating system dependency", severity="warning"),
    ]
    result = analyze([Source("src/é #%.py", 'x = "😀"; import os\n')], rules)

    document = to_sarif(result, rules)
    [run] = document["runs"]
    driver = run["tool"]["driver"]
    assert document["version"] == "2.1.0"
    assert driver["name"] == "Invariant"
    assert driver["version"] == __version__
    assert [rule["id"] for rule in driver["rules"]] == ["no-os", "z-size"]
    assert driver["rules"][0]["shortDescription"] == {
        "text": "No operating system dependency"
    }
    assert driver["rules"][0]["defaultConfiguration"] == {
        "enabled": True,
        "level": "warning",
    }
    assert run["columnKind"] == "unicodeCodePoints"
    [finding] = run["results"]
    assert (finding["ruleId"], finding["ruleIndex"], finding["level"]) == (
        "no-os",
        0,
        "warning",
    )
    assert finding["locations"] == [
        {
            "physicalLocation": {
                "artifactLocation": {"uri": "src/%C3%A9%20%23%25.py"},
                "region": {"startLine": 1, "startColumn": 10},
            }
        }
    ]
    assert run["properties"]["outcome"] == "pass"
    assert run["invocations"] == [{"executionSuccessful": True}]


def test_sarif_keeps_findings_and_reports_incomplete_files_as_notifications():
    rules = [import_rule()]
    result = analyze(
        [Source("bad.py", "def bad(:\n"), Source("violation.py", "import os\n")],
        rules,
        unreadable=[Unreadable("large.py", "too_large")],
    )

    [run] = to_sarif(result, rules)["runs"]

    assert run["properties"]["outcome"] == "fail"
    assert len(run["results"]) == 1
    [invocation] = run["invocations"]
    assert invocation["executionSuccessful"] is False
    assert [
        item["properties"]["code"] for item in invocation["toolExecutionNotifications"]
    ] == ["syntax_error", "too_large"]
    first = invocation["toolExecutionNotifications"][0]
    assert first["locations"][0]["physicalLocation"]["region"]["startLine"] == 1


def test_sarif_never_emits_invalid_source_paths_as_artifact_uris():
    result = analyze([], [], unreadable=[Unreadable("../outside.py", "invalid_path")])

    [run] = to_sarif(result, [])["runs"]

    [notification] = run["invocations"][0]["toolExecutionNotifications"]
    assert "locations" not in notification
    assert notification["properties"]["path"] == "../outside.py"
    assert run["results"] == []


@pytest.mark.parametrize(
    ("content", "expected_exit", "outcome"),
    [
        ("import json\n", 0, "pass"),
        ("import os\n", 1, "fail"),
        ("def bad(:", 2, "incomplete"),
    ],
)
def test_cli_sarif_is_one_json_document_and_preserves_exit_codes(
    tmp_path, capsys, content, expected_exit, outcome
):
    (tmp_path / "invariant.toml").write_text(
        'version = 1\n[[rules]]\nkey = "no-os"\n'
        'rule_type = "forbidden_import"\nforbidden_imports = ["os"]\n',
        encoding="utf-8",
    )
    (tmp_path / "code.py").write_text(content, encoding="utf-8")

    code = main(["check", str(tmp_path), "--format", "sarif"])

    captured = capsys.readouterr()
    assert code == expected_exit
    assert captured.err == ""
    [run] = json.loads(captured.out)["runs"]
    assert run["properties"]["outcome"] == outcome
    assert run["properties"]["source"]["mode"] == "directory"


def test_sarif_not_applicable_preserves_no_verdict_and_disabled_metadata():
    rules = [import_rule(enabled=False)]
    result = analyze([Source("code.py", "import os\n")], rules)

    [run] = to_sarif(result, rules)["runs"]

    assert run["properties"]["outcome"] == "not_applicable"
    assert run["results"] == []
    assert run["tool"]["driver"]["rules"][0]["defaultConfiguration"]["enabled"] is False
