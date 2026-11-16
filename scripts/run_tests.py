"""Run the unit tests in parallel: ./scripts/run_tests.py [-j N] [pattern]

Spreads the individual tests over N worker processes (default: CPU count, at most 8), each one a plain
`python -m unittest` run, so every test still gets its own temp project. Same tests and result as
`python -m unittest discover -s tests`, in a fraction of the time. A pattern keeps only test ids that
contain it (e.g. `editor`, `test_grade.StagingTests`)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ids(pattern: str | None) -> list[str]:
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

    def walk(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from walk(item)
            else:
                yield item.id()

    ids = sorted(walk(unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))))
    broken = [i for i in ids if i.startswith("unittest.loader")]
    if broken:
        raise SystemExit(f"tests failed to import: {', '.join(broken)} — run python -m unittest discover -s tests to see why")
    return [i for i in ids if not pattern or pattern in i]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 2))
    ap.add_argument("pattern", nargs="?")
    args = ap.parse_args()
    ids = test_ids(args.pattern)
    if not ids:
        print("no tests match")
        return 1
    jobs = max(1, min(args.jobs, len(ids)))
    chunks = [ids[i::jobs] for i in range(jobs)]  # round-robin: each module's slow tests land on different workers
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "scripts"), str(ROOT / "tests"), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep))
    start = time.monotonic()

    def run(chunk: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "unittest", "-q", *chunk], cwd=ROOT, env=env,
                              capture_output=True, text=True)  # fmt: skip

    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(run, chunks))
    failed = [r for r in results if r.returncode != 0]
    for r in failed:
        # unittest's summary and tracebacks are on stderr; the tests' own progress chatter is dropped
        print(r.stderr[r.stderr.find("=" * 70):] if "=" * 70 in r.stderr else r.stderr)
    took = time.monotonic() - start
    print(f"{len(ids)} tests on {jobs} workers in {took:.1f}s: " + ("OK" if not failed else f"FAILED ({len(failed)} worker(s) had failures)"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
