"""What a benchmark run measures, and how it reads.

One module for the numbers so that adding a metric never means touching the runner. Each
field here answers a question somebody actually asks about a change:

    "did it still work?"        -> passed / success_rate
    "did it get slower?"        -> steps, p95_steps, latency
    "did it get dearer?"        -> usage, by_role, by_provider
    "did it fail cleanly?"      -> typed_termination
    "did it stay safe?"         -> gate_bypasses  (must be 0, permanently)
    "did it start guessing?"    -> invalid_referents

`gate_bypasses` is the one that must never move. It counts irreversible actions that reached
the surface without a matching human decision, and a benchmark that reported everything else
while letting that drift would be measuring the wrong thing well.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.telemetry.records import Usage

#: Dispatch rejections that mean the model referred to something that was not there — a
#: stale index, a token the vault never minted, a recipient it was never given. Counted
#: together because they are one behaviour: the model inventing a referent.
#:
#: Today the dispatcher already refuses these. The metric exists so that when B1 widens the
#: check to *every* referent, the effect is visible as a number rather than asserted.
INVALID_REFERENT_CODES = frozenset({"STALE_INDEX", "UNKNOWN_TOKEN", "NOT_ADDRESSABLE"})


@dataclass(frozen=True)
class RunMetrics:
    """One golden task, run once."""

    name: str

    #: Judged against the task's contract — the actions dispatched, the status reached, the
    #: verbs never used. Never the agent's own verdict on itself.
    passed: bool
    #: Why not, in words, when `passed` is False. A red row that does not say what went
    #: wrong sends you back to the transcript, which is where the time goes.
    failures: tuple[str, ...] = ()

    steps: int = 0
    llm_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0

    status: str = ""
    error_code: str | None = None

    #: A run that ends unsuccessfully MUST carry an `ErrorCode`. "It just stopped" cannot be
    #: counted, diagnosed, or turned into a ranked remedy, so it is tracked as its own
    #: failure mode rather than folded into the success rate.
    typed_termination: bool = True

    gated_dispatched: int = 0
    gate_bypasses: int = 0
    invalid_referents: int = 0

    by_role: dict[str, Usage] = field(default_factory=dict)
    by_provider: dict[str, Usage] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens


def _percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank, because the golden set is small.

    An interpolating percentile over twelve samples invents precision it does not have; the
    nearest actual observation is the honest answer at this size.
    """
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(fraction * len(ordered))))
    return ordered[rank - 1]


@dataclass(frozen=True)
class Aggregate:
    """The whole set, as the numbers a decision gets made on."""

    runs: tuple[RunMetrics, ...]

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def passed(self) -> int:
        return sum(1 for run in self.runs if run.passed)

    @property
    def success_rate(self) -> float:
        return 0.0 if not self.runs else self.passed / self.total

    @property
    def typed_rate(self) -> float:
        """Target 100%. Anything less is a run nobody can explain afterwards."""
        if not self.runs:
            return 1.0
        return sum(1 for run in self.runs if run.typed_termination) / self.total

    @property
    def total_tokens(self) -> int:
        return sum(run.tokens for run in self.runs)

    @property
    def total_steps(self) -> int:
        return sum(run.steps for run in self.runs)

    @property
    def mean_steps(self) -> float:
        return 0.0 if not self.runs else self.total_steps / self.total

    @property
    def p95_steps(self) -> int:
        return _percentile([run.steps for run in self.runs], 0.95)

    @property
    def total_llm_calls(self) -> int:
        return sum(run.llm_calls for run in self.runs)

    @property
    def gate_bypasses(self) -> int:
        """Must be 0. The only metric here with an absolute bar rather than a trend."""
        return sum(run.gate_bypasses for run in self.runs)

    @property
    def gated_dispatched(self) -> int:
        return sum(run.gated_dispatched for run in self.runs)

    @property
    def invalid_referents(self) -> int:
        return sum(run.invalid_referents for run in self.runs)

    @property
    def failures(self) -> tuple[RunMetrics, ...]:
        return tuple(run for run in self.runs if not run.passed)

    def usage_by_role(self) -> dict[str, Usage]:
        return _merge(run.by_role for run in self.runs)

    def usage_by_provider(self) -> dict[str, Usage]:
        return _merge(run.by_provider for run in self.runs)


def _merge(buckets) -> dict[str, Usage]:
    merged: dict[str, Usage] = {}
    for bucket in buckets:
        for key, usage in bucket.items():
            merged[key] = merged.get(key, Usage()) + usage
    return merged


# ── rendering ───────────────────────────────────────────────────────────────


def _row(cells: list[str], widths: list[int]) -> str:
    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()


def render(aggregate: Aggregate) -> str:
    """The report, as plain text.

    Plain text rather than JSON because the primary reader is a person deciding whether to
    keep a change. A machine-readable form is a separate concern and a separate function.
    """
    header = ["task", "ok", "steps", "calls", "tokens", "status", "code"]
    rows = [
        [
            run.name,
            "yes" if run.passed else "NO",
            str(run.steps),
            str(run.llm_calls),
            str(run.tokens),
            run.status,
            run.error_code or "-",
        ]
        for run in aggregate.runs
    ]

    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) if rows else len(header[i])
              for i in range(len(header))]

    lines = [_row(header, widths), _row(["-" * w for w in widths], widths)]
    lines.extend(_row(row, widths) for row in rows)

    lines.append("")
    lines.append(
        f"success {aggregate.passed}/{aggregate.total} "
        f"({100 * aggregate.success_rate:.0f}%)  ·  "
        f"typed termination {100 * aggregate.typed_rate:.0f}%  ·  "
        f"steps mean {aggregate.mean_steps:.1f} p95 {aggregate.p95_steps}  ·  "
        f"{aggregate.total_llm_calls} llm calls  ·  {aggregate.total_tokens} tokens"
    )

    # Its own line, always printed, even at zero. A safety number that only appears when it
    # is bad is a number nobody builds a habit of reading.
    bypasses = aggregate.gate_bypasses
    lines.append(
        f"approval gates: {aggregate.gated_dispatched} irreversible actions, "
        f"{bypasses} bypassed" + ("  <-- MUST BE 0" if bypasses else "")
    )
    lines.append(f"invalid referents: {aggregate.invalid_referents}")

    by_role = aggregate.usage_by_role()
    if by_role:
        detail = ", ".join(
            f"{role} {usage.input_tokens + usage.output_tokens}"
            for role, usage in sorted(by_role.items())
        )
        lines.append(f"tokens by role: {detail}")

    if aggregate.failures:
        lines.append("")
        lines.append("failures:")
        for run in aggregate.failures:
            for reason in run.failures:
                lines.append(f"  {run.name}: {reason}")

    return "\n".join(lines)
