import ast

import pytest

from invariant.calls import resolve_calls
from invariant.config import ConfigError, ForbiddenCallRule, parse_config
from invariant.engine import Source, analyze
from invariant.result import Outcome


def targets(source: str, package: tuple[str, ...] = ("app", "domain")) -> list[str]:
    return [call.target for call in resolve_calls(ast.parse(source), package)]


def forbid_calls(*calls: str, **fields) -> ForbiddenCallRule:
    return ForbiddenCallRule(
        rule_type="forbidden_call", key="calls", forbidden_calls=list(calls), **fields
    )


def called(source: str, *calls: str) -> list[str]:
    result = analyze([Source("app/domain/service.py", source)], [forbid_calls(*calls)])
    return [item.data["call"] for item in result.violations]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os as operating\noperating.system('x')\n", ["os.system"]),
        ("from os import system as sh\nsh('x')\n", ["os.system"]),
        ("import os\nos.system('x')\n", ["os.system"]),
        (
            "import os.path\nos.path.join('a')\nos.system('b')\n",
            ["os.path.join", "os.system"],
        ),
        ("import os.path as p\np.join('a')\n", ["os.path.join"]),
        ("eval('1')\n", ["builtins.eval"]),
        ("import builtins\nbuiltins.eval('1')\n", ["builtins.eval"]),
        ("from . import helpers\nhelpers.run()\n", ["app.domain.helpers.run"]),
        (
            "def f():\n    import subprocess as sp\n    sp.run(['x'])\n",
            ["subprocess.run"],
        ),
        (
            "import functools\n@functools.lru_cache(maxsize=1)\ndef f(): pass\n",
            ["functools.lru_cache"],
        ),
        ("[eval(x) for x in data]\n", ["builtins.eval"]),
        ("import functools\n@functools.cache\ndef f(): pass\n", ["functools.cache"]),
        (
            "from dataclasses import dataclass\n@dataclass\nclass A: pass\n",
            ["dataclasses.dataclass"],
        ),
    ],
)
def test_calls_resolve_through_imports_aliases_and_builtins(source, expected):
    assert targets(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef f(os):\n    os.system('x')\n",
        "def f(eval):\n    eval('x')\n",
        "eval = print\neval('x')\n",
        "f = lambda os: os.system('x')\n",
        "import os\ndef f():\n    os = fake()\n    os.system('x')\n",
        "import os\nclass A:\n    os = 1\n    y = os.system('z')\n",
        "import os\ndef f():\n    global os\n    os = 1\nos.system('x')\n",
        "def outer():\n    import os\n    def inner():\n        nonlocal os\n        os = 1\n    os.system('x')\n",
        "try:\n    import ujson as json\nexcept ImportError:\n    json = None\njson.loads('1')\n",
        "import os as x\nimport sys as x\nx.exit()\n",
        "from os import *\nsystem('x')\nopen('f')\n",
        "import os\nfrom helpers import *\nos.system('x')\n",
        "import os\n[os.system(c) for os in items]\n",
        "import os\n[(os := y) for y in z]\nos.system('x')\n",
        "import os\nwith connect() as os:\n    os.system('x')\n",
        "import os\nfor os in items:\n    os.system('x')\n",
        "import os\ntry:\n    pass\nexcept Exception as os:\n    os.system('x')\n",
        "import os\ndel os\nos.system('x')\n",
        "system = os.system\nsystem('x')\n",
        "get_os().system('x')\n",
        "undefined_name('x')\n",
        "import os\ndef f[os]():\n    os.system('x')\n",
        "import os\nclass A[os]:\n    def m(self):\n        os.system('x')\n",
        "import os\ntype X[os] = os.system('x')\n",
    ],
)
def test_shadowed_or_dynamic_names_are_not_claimed(source):
    assert targets(source) == []


def test_class_variables_do_not_shadow_names_inside_methods():
    source = (
        "import os\nclass A:\n    os = 1\n    def m(self):\n        os.system('x')\n"
    )

    assert targets(source) == ["os.system"]


def test_defaults_and_decorators_resolve_in_the_enclosing_scope():
    source = "import os\ndef f(os, cwd=os.getcwd()):\n    os.system('x')\n"

    assert targets(source) == ["os.getcwd"]


def test_type_parameters_are_seen_by_annotations_and_the_body_only():
    source = (
        "import os\n"
        "@os.fspath\n"
        "def f[os](path: os.PathLike(), cwd=os.getcwd()):\n"
        "    os.system('x')\n"
    )

    assert targets(source) == ["os.fspath", "os.getcwd"]


def test_long_expressions_do_not_exhaust_the_stack():
    # 20,000 additions nest 20,000 levels deep in the syntax tree.
    source = "x = " + " + ".join(["eval('1')"] * 20_000) + "\n"

    assert targets(source) == ["builtins.eval"] * 20_000


def test_forbidden_call_reports_the_acceptance_cases():
    source = (
        "import os as operating\n"
        "from os import system as sh\n"
        "operating.system('a')\n"
        "sh('b')\n"
        "eval('c')\n"
        "def safe(eval):\n"
        "    eval('d')\n"
    )

    assert called(source, "os.system", "builtins.eval") == [
        "os.system",
        "os.system",
        "builtins.eval",
    ]


def test_an_entry_covers_everything_inside_it():
    source = (
        "import subprocess\nimport subprocessx\n"
        "subprocess.run(['a'])\nsubprocess.Popen(['b'])\nsubprocessx.run()\n"
    )

    assert called(source, "subprocess") == ["subprocess.run", "subprocess.Popen"]


def test_forbidden_call_diagnostic_points_at_the_call():
    result = analyze(
        [Source("app/domain/service.py", 'x = "é"; eval("1")\n')],
        [forbid_calls("builtins.eval", name="No eval", severity="warning")],
    )

    [violation] = result.violations
    assert (violation.line, violation.column) == (1, 10)
    assert violation.code == "forbidden_call"
    assert violation.message == (
        "app.domain.service must not call builtins.eval (called builtins.eval)"
    )
    assert violation.data == {"call": "builtins.eval", "forbidden": "builtins.eval"}
    assert result.outcome is Outcome.PASS  # a warning, below the default threshold


def test_forbidden_call_respects_source_pattern_and_exclusions():
    rule = forbid_calls(
        "builtins.eval",
        source_pattern="app.domain.*",
        exclude_paths=["app/domain/legacy/"],
    )
    sources = [
        Source("app/domain/a.py", "eval('1')\n"),
        Source("app/domain/legacy/b.py", "eval('1')\n"),
        Source("app/api.py", "eval('1')\n"),
    ]

    result = analyze(sources, [rule])

    assert [item.path for item in result.violations] == ["app/domain/a.py"]


def test_forbidden_call_configuration_is_validated():
    config = parse_config(
        'version = 1\n[[rules]]\nkey = "no-eval"\nrule_type = "forbidden_call"\n'
        'forbidden_calls = ["builtins.eval", "os.system"]\n'
    )
    assert config.rules[0].forbidden_calls == ["builtins.eval", "os.system"]

    for calls in ("[]", '["os.system", "os.system"]', '["os system"]'):
        with pytest.raises(ConfigError):
            parse_config(
                'version = 1\n[[rules]]\nkey = "x"\nrule_type = "forbidden_call"\n'
                f"forbidden_calls = {calls}\n"
            )
