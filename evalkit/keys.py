"""A manifest of the content keys a score was computed from. Pure.

One row per job of the current suite: its key, what it names, and the stored run the
key selected, by CLI version and a hash of the trace. Someone holding the suite can
recompute the keys with ``plan.jobs`` and someone holding the store can find each run
by key and check its hash, both without calling a model. The manifest holds no prompt,
case input or output, so it can be archived beside a report.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from evalkit.engine import Outcome
from evalkit.plan import Job
from evalkit.spec import Suite
from evalkit.store import JsonlStore
from evalkit.trace import Trace

COLUMNS = ("# key", "variant", "case", "sample", "cli_version", "trace_sha256", "result")
_NONE = "-"


def manifest(suite: Suite, jobs: Sequence[Job], store: JsonlStore,
             outcomes: Sequence[Outcome]) -> str:
    """The manifest text: a header naming the suite file by hash, then one row per job.

    ``result`` is ``pass`` or ``fail`` for a scored run, ``unjudged`` for a stored run
    still waiting for a verdict, and ``missing`` when the key has no stored run.
    """
    scored = {(o.variant, o.case, o.sample): o.passed for o in outcomes}
    digest = hashlib.sha256(suite.path.read_bytes()).hexdigest()
    lines = [f"# evalkit keys {suite.path.name} sha256={digest}", "\t".join(COLUMNS)]
    for job in jobs:
        lines.append("\t".join(_row(job, store, scored)))
    return "\n".join(lines) + "\n"


def trace_sha256(trace: Trace) -> str:
    """A hash of the trace's canonical JSON, to match a stored record exactly."""
    text = json.dumps(trace.to_json(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _row(job: Job, store: JsonlStore, scored: dict[tuple[str, str, int], bool]) -> list[str]:
    head = [job.key, job.variant.id, job.case.id, str(job.sample)]
    record = store.get(job.key)
    if record is None:
        return [*head, _NONE, _NONE, "missing"]
    passed = scored.get((job.variant.id, job.case.id, job.sample))
    result = "unjudged" if passed is None else ("pass" if passed else "fail")
    return [*head, record.cli_version, trace_sha256(record.trace), result]
