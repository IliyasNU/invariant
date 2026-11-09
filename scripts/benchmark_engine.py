"""Reproducible engine workloads, without network, database or native code.

Run with the project's Python, for example::

    uv run python scripts/benchmark_engine.py --output benchmark.json
    uv run python scripts/benchmark_engine.py --source-tree ../baseline \
        --output before.json --profile profiles/before --memory
    uv run python scripts/benchmark_engine.py --compare before.json

``--source-tree`` imports the engine from another checkout (before any Invariant
import), using this same workload generator and interpreter. It does not change
Git state. Run each revision in a separate process. Data creation, fingerprints,
and JSON serialization are outside timed regions. Profiling and memory tracing
are separate extra runs and never contaminate the uninstrumented timings.
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import hashlib
import importlib
import io
import json
import os
import platform
import pstats
import statistics
import subprocess
import sys
import time
import tracemalloc
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Workload:
    name: str
    description: str
    files: tuple[tuple[str, str], ...]
    rules: tuple[dict[str, Any], ...]
    max_violations: int | None = None


def _service(index: int, functions: int) -> str:
    """Ordinary pure service code: imports, functions and comprehensions."""
    imports = (
        "from dataclasses import dataclass\n"
        "from decimal import Decimal\n"
        "from collections.abc import Iterable\n"
        "import json\n"
        "import math\n\n"
        "@dataclass(frozen=True)\n"
        "class Invoice:\n"
        "    total: Decimal\n"
        "    currency: str\n\n"
    )
    return imports + "\n".join(
        f"def calculate_{index}_{number}(items: Iterable[dict], tax=Decimal('0.2')):\n"
        "    amounts = [Decimal(str(item['price'])) for item in items if item['active']]\n"
        "    subtotal = sum(amounts, Decimal('0'))\n"
        "    total = subtotal * (1 + tax)\n"
        "    return Invoice(total=total.quantize(Decimal('0.01')), currency='USD')\n"
        for number in range(functions)
    )


def _clean_rules(count: int) -> tuple[dict[str, Any], ...]:
    rules: list[dict[str, Any]] = [
        {
            "key": "absolute-imports",
            "rule_type": "no_relative_import",
            "severity": "warning",
        },
        {"key": "file-size", "rule_type": "max_file_lines", "max_lines": 1000},
        {
            "key": "no-shell",
            "rule_type": "forbidden_call",
            "forbidden_calls": ["os.system", "subprocess", "builtins.eval"],
        },
    ]
    # Multiple layer boundaries commonly apply to one module. Each checks the
    # actual AST, not an unrelated arithmetic microbenchmark.
    rules.extend(
        {
            "key": f"boundary-{number:03}",
            "rule_type": "forbidden_import",
            "source_pattern": "shop.domain.*",
            "forbidden_imports": [
                f"shop.infrastructure.adapter_{number}",
                f"shop.presentation.controller_{number}",
                "fastapi",
                "sqlalchemy",
            ],
        }
        for number in range(count - len(rules))
    )
    return tuple(rules)


def workloads() -> tuple[Workload, ...]:
    """Fixed inputs. No random generator, filesystem contents or clock input."""
    imports = "".join(
        f"import shop.infrastructure.adapter_{number % 8}.db as db_{number}\n"
        for number in range(240)
    )
    import_rules = tuple(
        {
            "key": f"isolation-{number:03}",
            "rule_type": "forbidden_import",
            "source_pattern": "shop.domain.*",
            "severity": "warning" if number % 4 == 0 else "error",
            "forbidden_imports": ["shop.infrastructure"],
        }
        for number in range(48)
    )
    calls = (
        "import os as operating\nimport subprocess as sp\nimport json\n\n"
        + "\n".join(
            f"def task_{number}(payload):\n"
            "    decoded = json.loads(payload)\n"
            "    operating.system(decoded['command'])\n"
            "    sp.run(decoded['arguments'], check=True)\n"
            "    return json.dumps(decoded)\n"
            for number in range(80)
        )
    )
    call_rules = tuple(
        {
            "key": f"no-process-{number:03}",
            "rule_type": "forbidden_call",
            "forbidden_calls": ["os.system", "subprocess"],
            "severity": "warning" if number % 3 == 0 else "error",
        }
        for number in range(12)
    )
    return (
        Workload(
            "small_clean",
            "12 service files, 8 architecture rules, no violations",
            tuple((f"shop/domain/service_{i}.py", _service(i, 6)) for i in range(12)),
            _clean_rules(8),
        ),
        Workload(
            "multi_rule_clean",
            "60 service files, 64 overlapping layer rules, no violations",
            tuple((f"shop/domain/service_{i}.py", _service(i, 18)) for i in range(60)),
            _clean_rules(64),
        ),
        Workload(
            "import_heavy_capped",
            "16 files with 240 imports each, 48 rules; list at most 1000 violations",
            tuple((f"shop/domain/legacy_{i}.py", imports) for i in range(16)),
            import_rules,
            1000,
        ),
        Workload(
            "call_heavy",
            "16 files with 80 functions and 320 calls each, 12 forbidden-call rules",
            tuple((f"shop/domain/tasks_{i}.py", calls) for i in range(16)),
            call_rules,
            1000,
        ),
        Workload(
            "dense_file_capped",
            "One generated 400 KB file with 40000 imports, 3 rules; list at most 1000 violations",
            (("shop/domain/generated.py", "import os\n" * 40000),),
            tuple(
                {
                    "key": f"no-os-{number}",
                    "rule_type": "forbidden_import",
                    "forbidden_imports": ["os"],
                }
                for number in range(3)
            ),
            1000,
        ),
    )


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def fingerprint(result: Any) -> dict[str, Any]:
    """Summary and full diagnostic hash: a faster but different result fails parity.

    Only engine_version is excluded from semantic comparison, so an intentional
    version bump does not hide performance parity. Diagnostics include message,
    location, rule snapshot and type-specific data, in their actual output order.
    """
    document = result.to_dict()
    diagnostics = document.pop("diagnostics")
    document.pop("engine_version", None)
    return {
        "summary": document,
        "diagnostics_count": len(diagnostics),
        "diagnostics_sha256": _digest(diagnostics),
        "result_sha256": _digest({**document, "diagnostics": diagnostics}),
    }


def benchmark(
    workload: Workload,
    *,
    repeats: int,
    warmups: int,
    profile: Path | None,
    memory: bool,
) -> dict[str, Any]:
    # Imported here so --source-tree decides which code is measured.
    from invariant.config import RULES
    from invariant.engine import Source, analyze

    sources = [Source(path, content) for path, content in workload.files]
    rules = [RULES.validate_python(rule) for rule in workload.rules]

    def run():
        return analyze(sources, rules, max_violations=workload.max_violations)

    def checked_run() -> dict[str, Any]:
        return fingerprint(run())

    expected = checked_run()
    for _ in range(warmups):
        if checked_run() != expected:
            raise RuntimeError(f"{workload.name}: nondeterministic warmup result")
    timings = []
    for _ in range(repeats):
        gc.collect()
        start = time.perf_counter()
        result = run()
        duration = time.perf_counter() - start
        # No tracing, digesting or serialization within timed regions.
        timings.append(duration)
        if fingerprint(result) != expected:
            raise RuntimeError(f"{workload.name}: nondeterministic timed result")
        del result

    report: dict[str, Any] = {
        "name": workload.name,
        "description": workload.description,
        "workload_sha256": _digest(
            {
                "files": workload.files,
                "rules": workload.rules,
                "cap": workload.max_violations,
            }
        ),
        "files": len(sources),
        "source_bytes": sum(len(source.content.encode("utf-8")) for source in sources),
        "rules": len(rules),
        "max_violations": workload.max_violations,
        "timing_seconds": {
            "median": statistics.median(timings),
            "min": min(timings),
            "samples": timings,
        },
        "result": expected,
    }
    if profile is not None:
        profile.mkdir(parents=True, exist_ok=True)
        profiler = cProfile.Profile()
        gc.collect()
        profiled_result = profiler.runcall(run)
        if fingerprint(profiled_result) != expected:
            raise RuntimeError(f"{workload.name}: profile result changed")
        destination = profile / f"{workload.name}.prof"
        profiler.dump_stats(str(destination))
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats(
            "cumulative"
        ).print_stats(25)
        report["profile"] = {
            "stats_file": str(destination),
            "cumulative_top25": stream.getvalue(),
        }
        del profiled_result
    if memory:
        gc.collect()
        tracemalloc.start()
        try:
            measured_result = run()
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        if fingerprint(measured_result) != expected:
            raise RuntimeError(f"{workload.name}: memory result changed")
        report["memory_bytes"] = {"retained": retained, "peak": peak}
    return report


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source-tree",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="checkout containing src/invariant; imported in this process",
    )
    parser.add_argument(
        "--case",
        choices=[case.name for case in workloads()],
        action="append",
        help="case to run; repeat option to select multiple (default: all)",
    )
    parser.add_argument(
        "--repeats",
        type=_positive,
        default=5,
        help="uninstrumented samples per case (default: 5)",
    )
    parser.add_argument(
        "--case-repeat",
        action="append",
        default=[],
        metavar="CASE=N",
        help="override repeat count for one case; may be repeated",
    )
    parser.add_argument(
        "--warmups",
        type=int,
        default=1,
        help="extra warmup runs (default: 1; minimum: 0)",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        metavar="DIRECTORY",
        help="separate cProfile run per case, writing .prof files",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="separate tracemalloc run per case (slower)",
    )
    parser.add_argument("--output", type=Path, help="write JSON here as well as stdout")
    parser.add_argument(
        "--compare",
        type=Path,
        help="compare parity and timings against a previous JSON artifact",
    )
    parser.add_argument("--label", default="", help="label for this measurement")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    tree = args.source_tree.resolve()
    if not (tree / "src" / "invariant" / "engine.py").is_file():
        parser.error("--source-tree must contain src/invariant/engine.py")
    if args.warmups < 0:
        parser.error("--warmups must be at least 0")
    cases = {case.name: case for case in workloads()}
    counts = dict.fromkeys(cases, args.repeats)
    for override in args.case_repeat:
        name, separator, value = override.partition("=")
        if not separator or name not in cases:
            parser.error("--case-repeat must be CASE=N for a listed case")
        try:
            counts[name] = _positive(value)
        except ValueError, argparse.ArgumentTypeError:
            parser.error("--case-repeat count must be an integer at least 1")
    sys.path.insert(0, str(tree / "src"))
    package = importlib.import_module("invariant")
    if not Path(package.__file__).resolve().is_relative_to(tree / "src"):
        parser.error(
            "Invariant is already imported from another tree: use a fresh process"
        )
    revision = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    dirty = subprocess.run(
        ["git", "-C", str(tree), "status", "--porcelain", "--", "src"],
        capture_output=True,
        text=True,
        check=False,
    )
    report: dict[str, Any] = {
        "benchmark_schema_version": 1,
        "label": args.label,
        "created_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "processor": platform.processor(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "source_tree": str(tree),
            "engine_version": package.__version__,
            "git_commit": revision.stdout.strip() if revision.returncode == 0 else None,
            "git_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
        },
        "method": {
            "warmups": args.warmups,
            "gc": "collect before each timed sample; enabled during analysis",
            "timing": "perf_counter; engine only; profile and tracemalloc in separate runs",
        },
        "cases": [
            benchmark(
                case,
                repeats=counts[case.name],
                warmups=args.warmups,
                profile=args.profile,
                memory=args.memory,
            )
            for case in cases.values()
            if args.case is None or case.name in args.case
        ],
    }
    parity = True
    if args.compare:
        prior = json.loads(args.compare.read_text(encoding="utf-8"))
        previous = {case["name"]: case for case in prior["cases"]}
        comparisons = []
        uncompared = []
        for current in report["cases"]:
            old = previous.get(current["name"])
            if old is None:
                # A case added after the baseline was recorded has nothing to
                # compare against; it is listed, not counted as a match.
                uncompared.append(current["name"])
                continue
            matched = (
                old["workload_sha256"] == current["workload_sha256"]
                and old["result"] == current["result"]
            )
            parity &= matched
            comparisons.append(
                {
                    "name": current["name"],
                    "parity": matched,
                    "median_speedup": old["timing_seconds"]["median"]
                    / current["timing_seconds"]["median"]
                    if matched
                    else None,
                }
            )
        # Comparing nothing proves nothing.
        parity &= bool(comparisons)
        report["comparison"] = {
            "baseline": str(args.compare),
            "parity": parity,
            "cases": comparisons,
            "uncompared": uncompared,
        }
    text = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if parity else 2


if __name__ == "__main__":
    raise SystemExit(main())
