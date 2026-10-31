"""Run the jobs a suite still needs, and score what is stored."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol

from evalkit.plan import Job
from evalkit.spec import Case, Variant
from evalkit.store import JsonlStore, Record
from evalkit.trace import Trace


class Runner(Protocol):
    def run(self, variant: Variant, case: Case) -> Trace: ...


@dataclass(frozen=True)
class Outcome:
    """One stored run, judged by its case's checks."""

    variant: str
    case: str
    sample: int
    passed: bool
    failed_checks: tuple[str, ...]
    triggered: bool | None  # None when the variant has no skill


def pending(jobs: Sequence[Job], store: JsonlStore, cli_version: str) -> list[Job]:
    """Jobs with no stored trace from this CLI version."""
    out = []
    for job in jobs:
        record = store.get(job.key)
        if record is None or record.cli_version != cli_version:
            out.append(job)
    return out


def execute(todo: Sequence[Job], runner: Runner, store: JsonlStore, cli_version: str,
            workers: int, report: Callable[[Job, Exception | None], None]) -> int:
    """Run *todo*; store each success. Failures are reported, not stored, so the
    next ``run`` retries them. Only this thread writes the store. Returns failures."""
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[Future[Trace], Job] = {
            pool.submit(runner.run, job.variant, job.case): job for job in todo
        }
        for future in as_completed(futures):
            job = futures[future]
            try:
                trace = future.result()
            except Exception as exc:  # a failed run must not stop the others
                failures += 1
                report(job, exc)
                continue
            store.put(Record(job.key, job.variant.id, job.case.id, job.sample, cli_version, trace))
            report(job, None)
    return failures


def score(jobs: Sequence[Job], store: JsonlStore) -> list[Outcome]:
    """Judge every job that has a stored trace. No model calls."""
    out = []
    for job in jobs:
        record = store.get(job.key)
        if record is None:
            continue
        failed = tuple(c.describe() for c in job.case.checks if not c.passes(record.trace))
        skill = job.variant.skill
        triggered = None if skill is None else skill.name in record.trace.skills_invoked
        out.append(Outcome(job.variant.id, job.case.id, job.sample, not failed, failed, triggered))
    return out
