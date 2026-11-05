"""A run lists at most MAX_LISTED_VIOLATIONS violations, and counts them all.

Each line of "import os" is a violation of a rule forbidding os, so a 2 MB
request held 200,000 of them: one analysis took 52 s and 1.3 GB, and its
response was 58 MB. A pull request could hold 50 MB of such files.
"""

import pytest

from app.analysis import MAX_LISTED_VIOLATIONS
from app.analysis_service import load_analysis_run
from app.database import SessionLocal
from app.feedback import feedback_body
from invariant.config import RULES
from invariant.engine import Source, analyze
from invariant.result import Outcome


def rule(**fields):
    return RULES.validate_python(
        {
            "rule_type": "forbidden_import",
            "key": fields.get("severity", "error"),
            "name": "No os",
            **fields,
        }
    )


NO_OS = rule(forbidden_imports=["os"], source_pattern="*")
NO_OS_WARNING = rule(forbidden_imports=["os"], source_pattern="*", severity="warning")
FILES = [Source("a.py", "import os\n" * 5), Source("b.py", "import os\n" * 2)]


def test_the_engine_lists_every_violation_by_default():
    result = analyze(FILES, [NO_OS])

    assert len(result.violations) == 7
    assert (result.error_count, result.omitted_errors) == (7, 0)


@pytest.mark.parametrize("limit", [0, 3, 6])
def test_violations_past_the_limit_are_counted_not_listed(limit):
    result = analyze(FILES, [NO_OS, NO_OS_WARNING], max_violations=limit)

    assert len(result.violations) == limit
    # The first ones found: a.py comes first, with ten of them.
    assert {item.path for item in result.violations} <= {"a.py"}
    assert (result.error_count, result.warning_count) == (7, 7)
    assert result.to_dict()["summary"] == {
        "errors": 7,
        "warnings": 7,
        "analysis_errors": 0,
    }
    assert result.outcome is Outcome.FAIL


def test_an_omitted_warning_still_fails_when_warnings_block():
    result = analyze(FILES, [NO_OS_WARNING], max_violations=0, fail_on="warning")

    assert result.violations == ()
    assert result.outcome is Outcome.FAIL
    assert analyze(FILES, [NO_OS_WARNING], max_violations=0).outcome is Outcome.PASS


def test_a_run_stores_the_first_violations_and_counts_them_all(client):
    project = client.post("/projects", json={"name": "Many"}).json()
    created = client.post(
        f"/projects/{project['id']}/rules",
        json={"name": "No os", "source_pattern": "*", "forbidden_imports": ["os"]},
    )
    assert created.status_code == 201
    found = MAX_LISTED_VIOLATIONS + 500

    response = client.post(
        f"/projects/{project['id']}/analyze",
        json={"files": [{"path": "many.py", "content": "import os\n" * found}]},
    )

    assert response.status_code == 200
    run = response.json()
    assert (run["outcome"], run["violation_count"]) == ("fail", found)
    assert run["summary"]["errors"] == found
    assert len(run["violations"]) == MAX_LISTED_VIOLATIONS
    diagnostics = client.get(
        f"/analysis-runs/{run['id']}/diagnostics", params={"limit": 1}
    )
    assert diagnostics.headers["X-Total-Count"] == str(MAX_LISTED_VIOLATIONS)
    result = client.get(f"/analysis-runs/{run['id']}/result").json()
    assert result["summary"]["errors"] == found
    assert len(result["diagnostics"]) == MAX_LISTED_VIOLATIONS

    # The pull request comment counts what is not listed, too.
    with SessionLocal() as db:
        body = feedback_body(load_analysis_run(db, run["id"]))
    assert f"… and {found - 10} more" in body
