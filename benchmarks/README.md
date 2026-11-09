# Performance evidence

These optional experiments are not imported by the application, included in
the wheel, or required to install Invariant. No C++ compiler is needed for
normal use or the Python test suite.

- `2026-11-09.json`: historical timing samples, result fingerprints, traced
  allocation counts and native-probe results used by
  [the performance report](../docs/performance.md). Machine-specific paths
  were omitted. These are measurements of the pre-hardening 1.1.1 code, not
  claims of new measurements for each later commit.
- `native_prefix_probe.cpp` and `native_prefix_probe.py`: the isolated native
  experiment used for that report. It compares only matching of previously
  extracted names, not whole-program analysis.

Run the standalone experiment from the repository root with a C++11 compiler:

```bash
python benchmarks/native_prefix_probe.py --mode executable --compile --compiler g++ --repeat 7
```

It verifies Python/C++ result parity before timing. The default compiler is
`g++` on PATH; `--compiler` also accepts an absolute path. The executable is
built next to the source and ignored by Git. For the experimental in-process
mode use `--mode library`; its compiler and Python architectures must match.
For example, a 32-bit MinGW DLL cannot load into a 64-bit Python process.

The prepacked kernel timing excludes transfer overhead. The complete process
timing includes serialization, process startup, dictionary construction, I/O
and result decoding. Neither number demonstrates the speed of an unbuilt
in-process extension or the application.

For actual engine measurements use `scripts/benchmark_engine.py` as described
in the report. Do not run benchmarks concurrently with tests or builds.
