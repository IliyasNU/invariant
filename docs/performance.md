# Engine performance and the C++ assessment

This page records the measurements behind the 1.1.1 engine optimizations and
the decision on whether to add native (C++) code. All numbers come from one
Windows machine and are only meaningful relative to each other; rerun the
commands below before relying on them elsewhere.

## Summary

- On the five benchmark workloads, the 1.1.1 engine is **1.7× to 14.7×
  faster** (median wall time) than 1.1.0 (commit `9d7af36`), with
  **identical results**: the full result document and the SHA-256 of every
  diagnostic match in every case.
- Peak memory traced by `tracemalloc` is **unchanged** (within +6%). It is set
  by parsing and the syntax tree, not by diagnostic objects.
- **C++ is not added.** After these changes, the only work a native kernel
  could take over without rewriting the parser is prefix matching on names
  already extracted from the AST. It is a small share of real analyses. On
  this machine a C++ kernel loses to Python once the data has to cross a
  process boundary, and an in-process build is not possible with the
  installed toolchain. The criteria for revisiting the decision are at the
  end of this page.

## What changed in 1.1.1

1. **One import index per file.** Before, every `forbidden_import` and
   `no_relative_import` rule called `ast.walk` over the whole tree for each
   file it applied to. With 64 rules on one module, the tree was walked 64
   times. `ParsedFile.imports` now walks it once and keeps the import nodes
   in the same breadth-first order, so the diagnostics kept under a limit do
   not change. The physical line count is cached in the same way.
2. **Prefixes prepared once per rule per analysis.** `PrefixMatcher` maps
   each forbidden prefix to its position in the configuration and checks a
   name's dotted ancestors (`a.b.c`, `a.b`, `a`). The cost depends on the
   name's depth, not the length of the prefix list. It still returns the
   *first configured* matching prefix and respects dotted boundaries (`pkg`
   does not match `pkgs`).
3. **No diagnostics past the limit.** With `max_violations`, the engine used
   to build every diagnostic (message, column, data) and then discard those
   past the limit. It now counts them, so verdicts and severity counts stay
   exact, but builds diagnostics only for the findings it lists.

No API, worker, configuration, result format or database schema changed.

## Method

`scripts/benchmark_engine.py` generates five fixed workloads in memory (no
randomness, network, database or filesystem input) and times
`invariant.engine.analyze` alone with `time.perf_counter`. Workload creation,
fingerprinting and JSON output happen outside the timed region. There is one
warm-up and a `gc.collect()` before each sample. cProfile and tracemalloc run
as separate, additional runs, so they never affect the timings.

Each case records a **fingerprint**: the result summary (without
`engine_version`), the diagnostic count, and SHA-256 hashes of the diagnostic
list and of the whole result. `--compare` fails with exit status 2 if any
fingerprint or workload hash differs from the baseline artifact. A faster but
different result is therefore never reported as a speedup. A case missing from
the baseline is listed under `uncompared`, and a comparison that matches no
case fails.

The baseline is commit `9d7af36` (1.1.0) in a separate detached worktree,
imported with `--source-tree`, measured with the same script and interpreter
in a separate process. (The artifact's `engine_version` field reads the
installed package metadata and shows 1.1.1 for both trees; `git_commit`
identifies the measured code.)

Environment: Windows 11 Pro 10.0.26200, CPython 3.14.5 (MSC, AMD64), the
project's `.venv`, 7 timed samples per case. Benchmarks ran on their own,
not alongside tests or other CPU-heavy work.

```powershell
# Baseline: commit 9d7af36 checked out in ..\invariant-perf-baseline
.\.venv\Scripts\python.exe scripts/benchmark_engine.py `
    --source-tree ../invariant-perf-baseline --repeats 7 --output baseline.json
# Optimized tree, with profiles and tracemalloc, compared against the baseline
.\.venv\Scripts\python.exe scripts/benchmark_engine.py --repeats 7 `
    --compare baseline.json --profile profiles-optimized --memory `
    --output optimized.json
```

## Workloads

| Case | Input | Rules | Limit |
| --- | --- | --- | --- |
| `small_clean` | 12 service files, 23 KB | 8, no violations | none |
| `multi_rule_clean` | 60 service files, 329 KB | 64 overlapping layer rules, no violations | none |
| `import_heavy_capped` | 16 files × 240 imports, 190 KB | 48 import rules; 184 320 violations | 1000 |
| `call_heavy` | 16 files × 320 calls, 224 KB | 12 call rules; 30 720 violations | 1000 |
| `dense_file_capped` | one 400 KB file with 40 000 imports | 3 import rules; 120 000 violations | 1000 |

## Results

Median and minimum of 7 uninstrumented samples, in seconds. "Parity" means
that the workload hash, summary, diagnostic count and both SHA-256 hashes are
identical to the baseline.

| Case | 1.1.0 median | 1.1.1 median | 1.1.0 min | 1.1.1 min | Median speedup | Parity |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `small_clean` | 0.0523 | 0.0193 | 0.0479 | 0.0186 | 2.70× | yes |
| `multi_rule_clean` | 4.4064 | 0.3002 | 4.0312 | 0.2967 | 14.68× | yes |
| `import_heavy_capped` | 0.8335 | 0.1823 | 0.7766 | 0.1802 | 4.57× | yes |
| `call_heavy` | 0.3475 | 0.2089 | 0.3263 | 0.2014 | 1.66× | yes |
| `dense_file_capped` | 0.5699 | 0.2046 | 0.5568 | 0.2013 | 2.79× | yes |

Diagnostic SHA-256 (first 16 hex digits, identical before and after):
`small_clean` and `multi_rule_clean` `4f53cda18c2baa0c` (no diagnostics),
`import_heavy_capped` `8f1c59ad8647519c`, `call_heavy` `a10bc5a28e9ea258`,
`dense_file_capped` `6d73317e12c49de9`.

Diagnostic objects built under the limit, counted by the profiler as calls to
`_violation`:

| Case | 1.1.0 | 1.1.1 | Listed |
| --- | ---: | ---: | ---: |
| `import_heavy_capped` | 184 320 | 1 000 | 1 000 |
| `call_heavy` | 30 720 | 1 000 | 1 000 |
| `dense_file_capped` | 120 000 | 1 000 | 1 000 |

### Memory

These numbers are Python allocations traced by `tracemalloc` during one
`analyze` call. "Retained" is measured while the result is still referenced.
This is **not** process RSS and excludes interpreter, allocator and native
library overhead.

| Case | Peak 1.1.0 | Peak 1.1.1 | Retained 1.1.0 | Retained 1.1.1 |
| --- | ---: | ---: | ---: | ---: |
| `small_clean` | 442 132 | 454 364 | 21 168 | 27 664 |
| `multi_rule_clean` | 1 255 944 | 1 303 016 | 37 104 | 45 016 |
| `import_heavy_capped` | 1 419 606 | 1 501 097 | 609 016 | 674 864 |
| `call_heavy` | 3 784 834 | 3 806 030 | 629 903 | 656 963 |
| `dense_file_capped` | 72 479 477 | 72 480 981 | 589 977 | 590 217 |

Bytes. The peak does not fall. In 1.1.0 the discarded diagnostics were
created and freed per file and rule, so they never set the peak; in
`dense_file_capped` the peak comes from parsing and holding the 40 000-node
tree. In 1.1.1 the peak is 0–6% higher (by 1.5–81 KB) and retained memory
is 0.2–66 KB higher; the source of that difference was not investigated. Building fewer diagnostics saves CPU time,
not peak memory.

### Where the time goes now

Share of cumulative time under cProfile (the profiler inflates code with many
small Python calls, so these are proportions, not wall times):

| Case | Call resolution (`resolve_calls`) | Import index (`ast.walk`) | Prefix matching | Parsing |
| --- | ---: | ---: | ---: | ---: |
| `small_clean` | 63% | 26% | 1.5% | 7% |
| `multi_rule_clean` | 62% | 24% | 3.8% | 5% |
| `import_heavy_capped` | — | 3% | 58% | 2% |
| `call_heavy` | 77% | — | 11% | 5% |
| `dense_file_capped` | — | 43% | 15% | 13% |

In 1.1.0, `ast.walk` took about 80% of `multi_rule_clean`. Now the remaining
cost is mostly Python-level traversal of the syntax tree: one walk for imports
and call resolution for `forbidden_call` rules. Prefix matching dominates only
`import_heavy_capped`, a synthetic worst case with 48 rules over 3 840 imports
of the same 8 modules.

## C++ assessment

### Scope

A native component was considered only as an optional, batched kernel for a
measured bottleneck *after* facts have been extracted from the AST: given a
list of dotted names and a rule's prefixes, return the first configured
matching prefix for each name. Rewriting the parser, the AST traversal, the
API or the worker in C++ was out of scope. The Python `ast` module would still
build the tree, so moving traversal to native code would mean reimplementing
Python parsing semantics.

### Experiment

`native_prefix_probe.cpp` and `native_prefix_probe.py` (kept with the
measurement artifacts, not in this repository) implement the same algorithm
as `PrefixMatcher` in both languages, a hash map of prefixes plus a walk over
dotted ancestors, and also a naive linear scan for reference. Inputs are
20 000 names and 100 prefixes, including overlapping prefixes, duplicates,
near-miss boundaries (`pkg1suffix`) and non-ASCII names. The C++ results
match both Python implementations for every name and for a separate set of
edge cases.

The available compiler is MinGW GCC 6.3.0, which targets 32-bit `mingw32`.
The Python runtime is 64-bit (AMD64). The shared library therefore builds as
an i386 PE32 DLL and cannot be loaded (`OSError: [WinError 193]`). The probe
was run as a standalone 32-bit executable instead:

```powershell
.\.venv\Scripts\python.exe -I native_prefix_probe.py --mode executable --compile --repeat 7
```

| Variant (20 000 names, 100 prefixes) | Median, ms |
| --- | ---: |
| Python, naive scan over prefixes | 99.52 |
| Python, ancestor dictionary (the 1.1.1 algorithm) | 9.09 |
| C++ kernel only: prebuilt map, prepacked input, no output conversion | 2.49 |
| C++ end to end: serialization, process start, I/O, map build, JSON decoding | 17.94 |

This is an isolated microbenchmark, not an application measurement.

- The algorithm mattered more than the language: the dictionary approach
  is 11× faster than the naive scan, in Python.
- The bare C++ kernel is 3.7× faster than the same algorithm in Python. Its
  samples vary between 1.1 and 3.8 ms.
- Once the cost of getting data in and out is included, C++ is 2× *slower*
  than Python. An in-process extension would avoid starting a process. It
  would still have to encode and pack every name and convert the results, an
  O(n) step in Python that costs about as much per name as the dictionary
  lookup it replaces. This could not be measured here, because no 64-bit
  compiler is available.

### Upper bound on application gain

Prefix matching is at most 4% of `multi_rule_clean` and 11–15% of the
call- and import-dense cases. Making it free would save at most that share.
In the most favourable synthetic case, `import_heavy_capped` (0.18 s), about
184 000 lookups at ~0.45 µs each come to roughly 0.08 s. A 3.7× faster kernel
with no boundary cost would save about 0.06 s. Real boundary costs and the
restructuring needed to batch names per file would cut into that. These are
estimates derived from the measurements above, not measured application
gains.

### Decision

Do not add C++ or any native dependency. Reasons:

1. The measured application bottleneck after 1.1.1 is AST traversal and call
   resolution in Python, which a post-extraction kernel does not touch.
2. The part a kernel could take over is small in realistic workloads. The
   only measured end-to-end native variant is slower than Python.
3. A native extension would need 64-bit builds and wheels for each supported
   platform and Python version, plus a pure-Python fallback, and it would make
   the engine harder to review. For now, a 1.6–15× speedup came from pure
   Python changes.

Revisit the decision only if all of the following hold:

- profiles of **real** repositories (not these synthetic cases) show
  post-extraction matching above roughly 30% of analysis time;
- cheaper Python options have been tried first, for example caching the
  match per distinct name within an analysis (names such as module imports
  repeat heavily) or sharing matchers between rules with identical prefixes;
- an in-process, 64-bit build (CPython C API, nanobind or pybind11), measured
  end to end through `scripts/benchmark_engine.py`, beats Python with
  `--compare` parity on every case.

If adopted, the kernel must stay optional: an extra that is imported only when
available, with the Python matcher as the reference implementation and a test
that both give identical results.

## Reproducing

Measurement artifacts for this page (`baseline-timing-final.json`,
`baseline.json`, `baseline-dense.json`, `optimized-final.json`,
`native-probe-executable.json`, cProfile files and the probe sources) were
kept outside the repository, in `outputs/invariant-performance-20261109`. To
measure again:

1. Check out the baseline revision in a separate worktree:
   `git worktree add --detach ../invariant-perf-baseline 9d7af36`.
2. Run the two commands under [Method](#method) from this checkout. Do not run
   tests or other heavy jobs at the same time.
3. Use `--case NAME` for one workload and `--case-repeat NAME=N` to change
   the number of samples for one case.

`tests/test_benchmark.py` runs the script end to end and checks that a
changed result fails `--compare`. `tests/test_engine_performance.py` checks
the structural properties that do not depend on timing: one tree walk per
file, one line count per file, configured prefix order, and no diagnostics
built past the limit.
