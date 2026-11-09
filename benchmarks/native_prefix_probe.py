"""A repeatable, isolated C++ experiment, not an application benchmark.

Run with Python 3.14 (stdlib only):
    python native_prefix_probe.py --compile
    python native_prefix_probe.py --mode library --compile --compiler g++

The native boundary gets pre-extracted module names, not Python AST objects.
Python and C++ both use a prefix dictionary and walk dotted ancestors. An
additional naive baseline demonstrates why algorithm choice matters. The
default runs a standalone executable: it also works when the installed C++
compiler cannot build a DLL with the same architecture as the Python runtime.
"""

import argparse
import ctypes
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path


def packed(values):
    offsets = [0]
    chunks = []
    for value in values:
        chunk = value.encode("utf-8")
        chunks.append(chunk)
        offsets.append(offsets[-1] + len(chunk))
    data = b"".join(chunks)
    return data, (ctypes.c_uint64 * len(offsets))(*offsets)


def naive(names, prefixes):
    return [
        next(
            (
                i
                for i, prefix in enumerate(prefixes)
                if name == prefix or name.startswith(prefix + ".")
            ),
            -1,
        )
        for name in names
    ]


def dictionary(prefixes):
    indices = {}
    for i, prefix in enumerate(prefixes):
        indices.setdefault(prefix, i)
    return indices


def python_ancestors(names, indices):
    result = []
    for name in names:
        first = None
        while True:
            candidate = indices.get(name)
            if candidate is not None and (first is None or candidate < first):
                first = candidate
            name, dot, _ = name.rpartition(".")
            if not dot:
                break
        result.append(-1 if first is None else first)
    return result


class Native:
    def __init__(self, path, prefixes):
        self.lib = ctypes.CDLL(str(path))
        pointer = ctypes.POINTER(ctypes.c_uint64)
        self.lib.prefix_matcher_create.argtypes = [
            ctypes.c_char_p,
            pointer,
            ctypes.c_uint64,
        ]
        self.lib.prefix_matcher_create.restype = ctypes.c_void_p
        self.lib.prefix_matcher_destroy.argtypes = [ctypes.c_void_p]
        self.lib.prefix_matcher_destroy.restype = None
        self.lib.prefix_matcher_batch.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            pointer,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_int64),
        ]
        self.lib.prefix_matcher_batch.restype = ctypes.c_int
        data, offsets = packed(prefixes)
        self.matcher = self.lib.prefix_matcher_create(data, offsets, len(prefixes))
        if not self.matcher:
            raise MemoryError("The native matcher could not be built")

    def close(self):
        self.lib.prefix_matcher_destroy(self.matcher)
        self.matcher = None

    def kernel(self, data, offsets, output):
        code = self.lib.prefix_matcher_batch(
            self.matcher, data, offsets, len(output), output
        )
        if code != 0:
            raise RuntimeError("The native matcher failed")

    def batch(self, names):
        data, offsets = packed(names)
        output = (ctypes.c_int64 * len(names))()
        self.kernel(data, offsets, output)
        return list(output)


class NativeProcess:
    def __init__(self, path, prefixes):
        self.path = path
        self.prefixes = prefixes

    def batch(self, names, repeat=1, warmup=0):
        # Serialization, process startup, C++ input/matcher construction,
        # execution, output and Python JSON decoding are included by callers.
        data = (
            f"{len(self.prefixes)} {len(names)} {repeat} {warmup}\n"
            + "\n".join([*self.prefixes, *names])
            + "\n"
        ).encode("utf-8")
        completed = subprocess.run(
            [str(self.path)], input=data, capture_output=True, check=True, timeout=30
        )
        return json.loads(completed.stdout)


def samples(function, repeat):
    function()  # Untimed warm-up.
    values = []
    for _ in range(repeat):
        began = time.perf_counter()
        function()
        values.append((time.perf_counter() - began) * 1000)
    return {
        "median_ms": round(statistics.median(values), 4),
        "samples_ms": [round(value, 4) for value in values],
    }


def workload(count):
    prefixes = (
        [f"pkg{i}" for i in range(40)]
        + [f"pkg{i}.service" for i in range(40)]
        + [f"café{i}.模块" for i in range(20)]
    )
    random.Random(20261109).shuffle(prefixes)
    names = []
    for i in range(count):
        if i % 7 == 0:
            names.append(f"café{i % 25}.模块.actions.task{i % 17}")
        elif i % 11 == 0:
            names.append(f"pkg{i % 55}suffix.service.run")
        elif i % 13 == 0:
            names.append(f"pkg{i % 55}")
        else:
            names.append(f"pkg{i % 55}.service.handlers.task{i % 17}")
    return names, prefixes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile", action="store_true")
    parser.add_argument(
        "--mode", choices=("executable", "library"), default="executable"
    )
    parser.add_argument("--compiler", default="g++")
    parser.add_argument("--names", type=int, default=20_000)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if args.names < 1 or args.repeat < 1:
        parser.error("--names and --repeat must be positive")
    directory = Path(__file__).resolve().parent
    filename = "native_prefix_probe"
    if args.mode == "library":
        filename += ".dll" if sys.platform == "win32" else ".so"
    elif sys.platform == "win32":
        filename += ".exe"
    library = directory / filename
    command = [
        args.compiler,
        "-std=c++11",
        "-O3",
        "-static-libgcc",
        "-static-libstdc++",
        str(directory / "native_prefix_probe.cpp"),
        "-o",
        str(library),
    ]
    if args.mode == "library":
        command.insert(1, "-shared")
    else:
        command.insert(1, "-DPREFIX_PROBE_STANDALONE")
    if sys.platform != "win32" and args.mode == "library":
        command.insert(1, "-fPIC")
    if args.compile:
        subprocess.run(command, check=True, timeout=120)
    names, prefixes = workload(args.names)
    indices = dictionary(prefixes)
    if args.mode == "executable":
        process = NativeProcess(library, prefixes)
        expected = naive(names, prefixes)
        assert python_ancestors(names, indices) == expected
        measured = process.batch(names, args.repeat, warmup=1)
        assert measured["matches"] == expected
        edge_prefixes = ["pkg.child", "pkg", "café.模块", "pkg"]
        edge_names = [
            "pkg.child.fn",
            "pkg.fn",
            "pkg",
            "pkgs.fn",
            "café.模块.fn",
            "none",
        ]
        assert (
            NativeProcess(library, edge_prefixes).batch(edge_names)["matches"]
            == (python_ancestors(edge_names, dictionary(edge_prefixes)))
            == naive(edge_names, edge_prefixes)
            == [0, 1, 1, -1, 2, -1]
        )
        results = {
            "python_naive": samples(lambda: naive(names, prefixes), args.repeat),
            "python_ancestor_dictionary": samples(
                lambda: python_ancestors(names, indices), args.repeat
            ),
            "cpp_kernel_prepacked_no_output_conversion": {
                "median_ms": round(statistics.median(measured["kernel_ms"]), 4),
                "samples_ms": measured["kernel_ms"],
            },
            "cpp_full_process_including_serialization_startup_io_json": samples(
                lambda: process.batch(names, repeat=1), args.repeat
            ),
        }
        print(
            json.dumps(
                {
                    "experiment": "ISOLATED prefix matching; NOT an Invariant app speedup or extension benchmark",
                    "mode": args.mode,
                    "python": sys.version,
                    "platform": platform.platform(),
                    "compiler_target": subprocess.check_output(
                        [args.compiler, "-dumpmachine"], text=True
                    ).strip(),
                    "names": len(names),
                    "prefixes": len(prefixes),
                    "repeat": args.repeat,
                    "parity": "all names and edge cases match",
                    "timing_scope": "Python dictionary is prebuilt; native kernel has prebuilt dictionary; full process includes native dictionary construction",
                    "build_command": command,
                    "timings": results,
                },
                indent=2,
                ensure_ascii=True,
            )
        )
        return
    native = Native(library, prefixes)
    try:
        expected = naive(names, prefixes)
        assert python_ancestors(names, indices) == expected
        assert native.batch(names) == expected
        # Overlap, configured order, exact match, dotted boundaries, Unicode,
        # duplicate prefixes and a failed lookup.
        edge_prefixes = ["pkg.child", "pkg", "café.模块", "pkg"]
        edge_names = [
            "pkg.child.fn",
            "pkg.fn",
            "pkg",
            "pkgs.fn",
            "café.模块.fn",
            "none",
        ]
        edge = Native(library, edge_prefixes)
        try:
            assert (
                edge.batch(edge_names)
                == python_ancestors(edge_names, dictionary(edge_prefixes))
                == naive(edge_names, edge_prefixes)
                == [0, 1, 1, -1, 2, -1]
            )
        finally:
            edge.close()
        data, offsets = packed(names)
        output = (ctypes.c_int64 * len(names))()
        results = {
            "python_naive": samples(lambda: naive(names, prefixes), args.repeat),
            "python_ancestor_dictionary": samples(
                lambda: python_ancestors(names, indices), args.repeat
            ),
            "cpp_kernel_prepacked_no_output_conversion": samples(
                lambda: native.kernel(data, offsets, output), args.repeat
            ),
            "cpp_batch_including_utf8_packing_and_output_list": samples(
                lambda: native.batch(names), args.repeat
            ),
        }
        print(
            json.dumps(
                {
                    "experiment": "ISOLATED prefix matching; NOT an Invariant app speedup",
                    "python": sys.version,
                    "platform": platform.platform(),
                    "names": len(names),
                    "prefixes": len(prefixes),
                    "repeat": args.repeat,
                    "parity": "all names and edge cases match",
                    "timing_scope": "matcher construction excluded for both dictionaries; each batch returns configured-prefix indices",
                    "build_command": command,
                    "timings": results,
                },
                indent=2,
                ensure_ascii=True,
            )
        )
    finally:
        native.close()


if __name__ == "__main__":
    main()
