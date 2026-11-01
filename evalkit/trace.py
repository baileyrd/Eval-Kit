"""What one run of a variant on a case produced."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: str  # JSON text, kept as given so traces round-trip exactly


@dataclass(frozen=True)
class Trace:
    final_text: str
    tool_calls: tuple[ToolCall, ...]
    skills_invoked: tuple[str, ...]
    files: tuple[tuple[str, str | None], ...]  # path -> text; None when binary or too large
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    turns: int = 1  # more than one when a message, such as a notice, came after the answer

    def file_text(self, path: str) -> str | None:
        return dict(self.files).get(path)

    def has_file(self, path: str) -> bool:
        return any(name == path for name, _ in self.files)

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        if self.turns == 1:  # leave one-turn records, and their hashes, as they always were
            del data["turns"]
        return data

    @classmethod
    def from_json(cls, data: dict[str, object]) -> Trace:
        calls = data["tool_calls"]
        files = data["files"]
        assert isinstance(calls, list) and isinstance(files, list)
        return cls(
            final_text=str(data["final_text"]),
            tool_calls=tuple(ToolCall(str(c["name"]), str(c["input"])) for c in calls),
            skills_invoked=tuple(str(s) for s in _list(data["skills_invoked"])),
            files=tuple((str(p), None if t is None else str(t)) for p, t in files),
            input_tokens=_int(data["input_tokens"]),
            output_tokens=_int(data["output_tokens"]),
            cost_usd=float(str(data["cost_usd"])),
            duration_ms=_int(data["duration_ms"]),
            turns=_int(data.get("turns", 1)),  # absent in records made before it existed
        )


def dumps(trace: Trace) -> str:
    return json.dumps(trace.to_json(), ensure_ascii=False)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"expected a list, got {type(value).__name__}")
    return value


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise ValueError(f"expected an integer, got {type(value).__name__}")
    return value
