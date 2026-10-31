"""Model-graded checks: what to ask the judge, how to read its answer, and how
well it agrees with labels a person wrote. Pure; the model call is the runner's.

A verdict is cached under a key that covers the rubric, the judge model and the
exact trace it graded, so ``score`` never calls the model and a re-run trace is
judged afresh.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from evalkit import checks
from evalkit.plan import Job
from evalkit.spec import JudgeConfig
from evalkit.store import JsonlStore
from evalkit.trace import Trace, dumps

MAX_FILE_CHARS = 20_000  # per file shown to the judge
MAX_FILES = 20

SYSTEM = """\
You grade one output of an AI assistant against one criterion.

Judge only the criterion, not style or anything else the output does. Base the
verdict on the output and files shown; if the criterion needs evidence that is
not there, it fails.

Reply with only a JSON object and nothing else:
{"reason": "<one or two sentences>", "pass": true or false}"""

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class VerdictError(ValueError):
    """The judge's reply held no usable verdict."""


@dataclass(frozen=True)
class JudgeJob:
    key: str
    rubric: str
    trace: Trace
    label: str  # "variant case #sample", for progress lines


def verdict_key(rubric: str, judge: JudgeConfig, trace: Trace) -> str:
    payload = json.dumps([rubric, judge.model, judge.effort, dumps(trace)], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def prompt(rubric: str, trace: Trace) -> str:
    """The user turn for the judge: criterion, final reply, then files."""
    files = [(path, text) for path, text in trace.files if text is not None][:MAX_FILES]
    shown = "\n".join(
        f'<file path="{path}">\n{text[:MAX_FILE_CHARS]}\n</file>' for path, text in files
    )
    return (f"<criterion>\n{rubric}\n</criterion>\n\n"
            f"<final_reply>\n{trace.final_text}\n</final_reply>\n\n"
            f"<files>\n{shown or '(none)'}\n</files>")


def parse_verdict(reply: str) -> tuple[bool, str]:
    """Read ``{"pass": bool, "reason": str}`` from the judge's reply."""
    match = _JSON_OBJECT.search(reply)
    if not match:
        raise VerdictError(f"no JSON object in judge reply: {reply[:200]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise VerdictError(f"judge reply is not valid JSON: {exc}") from None
    passed = data.get("pass") if isinstance(data, dict) else None
    if not isinstance(passed, bool):
        raise VerdictError(f"judge reply has no boolean 'pass': {match.group(0)[:200]!r}")
    return passed, str(data.get("reason", ""))


def pending(jobs: Iterable[Job], store: JsonlStore, judge: JudgeConfig) -> list[JudgeJob]:
    """Judge calls needed for stored traces that have no cached verdict yet."""
    out: dict[str, JudgeJob] = {}
    for job in jobs:
        record = store.get(job.key)
        if record is None:
            continue
        for item in jobs_for(job, record.trace, judge):
            if store.get_verdict(item.key) is None:
                out.setdefault(item.key, item)
    return list(out.values())


def jobs_for(job: Job, trace: Trace, judge: JudgeConfig) -> list[JudgeJob]:
    """One judge call per rubric of the job's case, for *trace*."""
    label = f"{job.variant.id} {job.case.id} #{job.sample}"
    return [JudgeJob(verdict_key(rubric, judge, trace), rubric, trace, label)
            for rubric in _case_rubrics(job)]


def verdicts_for(job: Job, trace: Trace, store: JsonlStore,
                 judge: JudgeConfig) -> dict[str, tuple[bool, str]] | None:
    """Cached verdicts for every rubric of the job's case, or None if any is missing."""
    found: dict[str, tuple[bool, str]] = {}
    for rubric in _case_rubrics(job):
        verdict = store.get_verdict(verdict_key(rubric, judge, trace))
        if verdict is None:
            return None
        found[rubric] = (verdict.passed, verdict.reason)
    return found


@dataclass(frozen=True)
class Agreement:
    labelled: int
    agreed: int
    judge_too_lenient: int  # judge passed, person failed
    judge_too_strict: int  # judge failed, person passed

    @property
    def rate(self) -> float:
        return self.agreed / self.labelled if self.labelled else 0.0


def agreement(pairs: Sequence[tuple[bool, bool]]) -> Agreement:
    """Compare (judge, person) verdict pairs."""
    return Agreement(
        labelled=len(pairs),
        agreed=sum(1 for judge, person in pairs if judge == person),
        judge_too_lenient=sum(1 for judge, person in pairs if judge and not person),
        judge_too_strict=sum(1 for judge, person in pairs if person and not judge),
    )


def _case_rubrics(job: Job) -> tuple[str, ...]:
    return tuple(dict.fromkeys(r for check in job.case.checks for r in checks.rubrics(check)))
