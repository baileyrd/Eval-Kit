"""A notice that arrives after the answer must not replace the answer."""

import json
import unittest

from evalkit import runner
from evalkit.trace import Trace


def assistant(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def user_text(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text}]}})


def result(text: str, tokens: int = 1, cost: float = 0.1, ms: int = 10) -> str:
    return json.dumps({"type": "result", "subtype": "success", "result": text,
                       "usage": {"input_tokens": tokens, "output_tokens": tokens},
                       "total_cost_usd": cost, "duration_ms": ms})


INIT = json.dumps({"type": "system", "subtype": "init", "skills": []})
TOOL_RESULT = json.dumps({"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})


class LateNoticeTest(unittest.TestCase):
    def test_a_second_result_keeps_the_first_answer_and_sums_usage(self) -> None:
        parsed = runner.parse_stream([
            INIT, assistant("REVIEW"), result("REVIEW", 5, 0.5, 100),
            user_text("<task-notification>done</task-notification>"),
            assistant("Noted; the four P2 findings above stand."),
            result("Noted; the four P2 findings above stand.", 2, 0.25, 30),
        ])
        self.assertEqual(parsed.final_text, "REVIEW")
        self.assertEqual(parsed.turns, 2)
        self.assertEqual((parsed.input_tokens, parsed.output_tokens), (7, 7))
        self.assertEqual((parsed.cost_usd, parsed.duration_ms), (0.75, 130))

    def test_an_injected_message_before_the_only_result_keeps_the_first_turn(self) -> None:
        parsed = runner.parse_stream([
            INIT, assistant("REVIEW"), user_text("background task finished"),
            assistant("Thanks."), result("Thanks."),
        ])
        self.assertEqual(parsed.final_text, "REVIEW")
        self.assertEqual(parsed.turns, 2)

    def test_tool_results_are_not_new_turns(self) -> None:
        parsed = runner.parse_stream([
            INIT, assistant("Let me look."), TOOL_RESULT, assistant("REVIEW"), result("REVIEW"),
        ])
        self.assertEqual(parsed.final_text, "REVIEW")
        self.assertEqual(parsed.turns, 1)

    def test_turns_round_trip_and_old_records_load_as_one(self) -> None:
        trace = Trace("x", (), (), (), 1, 1, 0.0, 1, turns=2)
        self.assertEqual(Trace.from_json(trace.to_json()), trace)
        old = trace.to_json()
        del old["turns"]
        self.assertEqual(Trace.from_json(old).turns, 1)


if __name__ == "__main__":
    unittest.main()
