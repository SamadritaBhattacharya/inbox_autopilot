"""`python -m tests.bench.run` — print the benchmark table.

A CLI rather than only a test because the two readers want different things. CI wants a
pass/fail on the invariants; a person deciding whether to keep a change wants the numbers
side by side with last week's. Same harness, two front doors.

Exit code is 1 when any task fails or any approval gate was bypassed, so this is usable as
a gate in a script without parsing the output.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from tests.bench.harness import run_all
from tests.bench.metrics import render
from tests.bench.tasks import GOLDEN


async def _main(selected: str | None) -> int:
    tasks = GOLDEN
    if selected:
        tasks = tuple(task for task in GOLDEN if selected in task.name)
        if not tasks:
            print(f"no golden task matches {selected!r}", file=sys.stderr)
            return 2

    aggregate = await run_all(tasks)
    print(render(aggregate))

    if aggregate.gate_bypasses:
        print("\nFAILED: an irreversible action dispatched without approval.", file=sys.stderr)
        return 1
    if aggregate.failures:
        print(f"\nFAILED: {len(aggregate.failures)} of {aggregate.total} tasks.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the golden-task benchmark.")
    parser.add_argument("-k", "--select", help="only tasks whose name contains this")
    args = parser.parse_args()
    return asyncio.run(_main(args.select))


if __name__ == "__main__":
    raise SystemExit(main())
