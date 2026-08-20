"""The `TrajectoryStore` port.

The ordered `StepRecord`s of a run ARE its trajectory: replayable, auditable, and the
substrate the benchmark measures. Anything worth asking about a run afterwards has to be
here, because nothing else survives the process.

This is a **persisted egress point**, so every write passes through PII redaction. A
trajectory that quietly accumulates real addresses would undo the vault one row at a time.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Protocol, runtime_checkable

from app.telemetry.records import StepRecord


@runtime_checkable
class TrajectoryStore(Protocol):
    async def save(self, thread_id: str, record: StepRecord) -> None: ...
    async def load(self, thread_id: str) -> list[StepRecord]: ...


class InMemoryTrajectoryStore:
    """Dev and test implementation. Swapped for a durable store in prod.

    Keyed by `thread_id` — the same key the checkpointer uses, so a run's state and its
    trajectory can always be lined up.
    """

    def __init__(self) -> None:
        self._records: dict[str, list[StepRecord]] = defaultdict(list)

    async def save(self, thread_id: str, record: StepRecord) -> None:
        self._records[thread_id].append(record)

    async def save_many(self, thread_id: str, records: list[StepRecord]) -> None:
        self._records[thread_id].extend(records)

    async def load(self, thread_id: str) -> list[StepRecord]:
        return list(self._records[thread_id])

    def thread_ids(self) -> list[str]:
        return list(self._records)
