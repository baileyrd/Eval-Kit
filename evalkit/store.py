"""Append-only JSONL store of traces and judge verdicts, each keyed by content.

One process writes it. Each line is one record; a later record for the same key
replaces an earlier one when read, so re-runs never rewrite history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evalkit.trace import Trace


@dataclass(frozen=True)
class Record:
    key: str
    variant: str
    case: str
    sample: int
    cli_version: str
    trace: Trace


@dataclass(frozen=True)
class Verdict:
    key: str
    passed: bool
    reason: str


class JsonlStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, Record] = {}
        self._verdicts: dict[str, Verdict] = {}
        self._load()

    def get(self, key: str) -> Record | None:
        return self._records.get(key)

    def get_verdict(self, key: str) -> Verdict | None:
        return self._verdicts.get(key)

    def put(self, record: Record) -> None:
        self._append({"key": record.key, "variant": record.variant, "case": record.case,
                      "sample": record.sample, "cli_version": record.cli_version,
                      "trace": record.trace.to_json()})
        self._records[record.key] = record

    def put_verdict(self, verdict: Verdict) -> None:
        self._append({"kind": "verdict", "key": verdict.key, "passed": verdict.passed,
                      "reason": verdict.reason})
        self._verdicts[verdict.key] = verdict

    def _append(self, data: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        if not self.path.exists():
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                self._load_line(json.loads(line))
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{self.path}:{number}: unreadable record: {exc}") from None

    def _load_line(self, data: dict[str, object]) -> None:
        key = str(data["key"])
        if data.get("kind") == "verdict":
            self._verdicts[key] = Verdict(key, data["passed"] is True, str(data["reason"]))
            return
        trace = data["trace"]
        if not isinstance(trace, dict):
            raise TypeError("trace must be an object")
        self._records[key] = Record(key, str(data["variant"]), str(data["case"]),
                                    int(str(data["sample"])), str(data["cli_version"]),
                                    Trace.from_json(trace))
