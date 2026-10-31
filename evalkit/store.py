"""Append-only JSONL store of traces, keyed by job.

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


class JsonlStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._records = self._load(path)

    def get(self, key: str) -> Record | None:
        return self._records.get(key)

    def put(self, record: Record) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"key": record.key, "variant": record.variant, "case": record.case,
                           "sample": record.sample, "cli_version": record.cli_version,
                           "trace": record.trace.to_json()}, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self._records[record.key] = record

    @staticmethod
    def _load(path: Path) -> dict[str, Record]:
        records: dict[str, Record] = {}
        if not path.exists():
            return records
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                record = Record(str(data["key"]), str(data["variant"]), str(data["case"]),
                                int(data["sample"]), str(data["cli_version"]),
                                Trace.from_json(data["trace"]))
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{number}: unreadable record: {exc}") from None
            records[record.key] = record
        return records
