"""The benchmark CLI must preserve its workload and results across runs."""

import json
import subprocess
import sys
from pathlib import Path


def test_benchmark_artifact_and_separate_process_parity(tmp_path):
    root = Path(__file__).resolve().parents[1]
    artifact = tmp_path / "baseline.json"
    command = [
        sys.executable,
        str(root / "scripts" / "benchmark_engine.py"),
        "--source-tree",
        str(root),
        "--case",
        "small_clean",
        "--repeats",
        "2",
        "--case-repeat",
        "small_clean=1",
        "--warmups",
        "0",
    ]
    baseline = subprocess.run(
        [*command, "--output", str(artifact)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    first = json.loads(baseline.stdout)
    assert first == json.loads(artifact.read_text(encoding="utf-8"))
    [case] = first["cases"]
    assert len(case["timing_seconds"]["samples"]) == 1
    assert case["result"]["summary"]["outcome"] == "pass"
    assert case["result"]["summary"]["files"]["analyzed"] == 12
    assert case["result"]["diagnostics_count"] == 0
    repeated = subprocess.run(
        [*command, "--compare", str(artifact)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    assert json.loads(repeated.stdout)["comparison"]["parity"] is True

    # A case added after the baseline is listed as uncompared, not as a match.
    added = subprocess.run(
        [*command, "--case", "dense_file_capped", "--compare", str(artifact)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    comparison = json.loads(added.stdout)["comparison"]
    assert comparison["parity"] is True
    assert comparison["uncompared"] == ["dense_file_capped"]
    assert [case["name"] for case in comparison["cases"]] == ["small_clean"]

    # Comparing no case at all proves nothing.
    nothing = subprocess.run(
        [
            *command[:4],
            "--case",
            "dense_file_capped",
            "--repeats",
            "1",
            "--warmups",
            "0",
            "--compare",
            str(artifact),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert nothing.returncode == 2
    assert json.loads(nothing.stdout)["comparison"]["parity"] is False

    # A changed summary cannot silently be accepted as a performance win.
    first["cases"][0]["result"]["summary"]["outcome"] = "fail"
    artifact.write_text(json.dumps(first), encoding="utf-8")
    mismatch = subprocess.run(
        [*command, "--compare", str(artifact)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert mismatch.returncode == 2
    assert json.loads(mismatch.stdout)["comparison"]["parity"] is False
