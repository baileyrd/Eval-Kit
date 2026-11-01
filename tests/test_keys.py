"""``score --keys`` archives which stored run each job's content key selected."""

import contextlib
import hashlib
import io
import json

from evalkit import engine, keys, plan
from evalkit.cli import main
from evalkit.spec import load_suite
from evalkit.store import JsonlStore, Record
from evalkit.trace import Trace
from tests.helpers import TempDirTest

SUITE = """\
samples = 2

[variant.control]

[[case]]
id     = "one"
input  = "hello"
checks = [{ regex = "OK" }]
"""


def trace(text: str) -> Trace:
    return Trace(text, (), (), (), 1, 1, 0.0, 1)


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class KeysManifestTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.suite_path = self.write("s.toml", SUITE)
        self.suite = load_suite(self.suite_path)
        self.jobs = plan.jobs(self.suite)
        self.store = JsonlStore(self.root / ".evalkit" / "s.jsonl")
        first = self.jobs[0]
        self.store.put(Record("stale-key", "control", "one", 0, "1.0", trace("old")))
        self.store.put(Record(first.key, "control", "one", 0, "2.1.316", trace("OK")))

    def manifest(self) -> str:
        outcomes = engine.score(self.jobs, self.store, self.suite.judge).outcomes
        return keys.manifest(self.suite, self.jobs, self.store, outcomes)

    def test_lists_every_job_with_its_key_record_and_result(self) -> None:
        text = self.manifest()
        header, columns, *rows = text.splitlines()
        digest = hashlib.sha256(self.suite_path.read_bytes()).hexdigest()
        self.assertEqual(header, f"# evalkit keys s.toml sha256={digest}")
        self.assertEqual(columns.split("\t"), ["# key", "variant", "case", "sample", "cli_version",
                                               "trace_sha256", "result"])
        first, second = (row.split("\t") for row in rows)
        trace_sha = hashlib.sha256(json.dumps(trace("OK").to_json(), sort_keys=True,
                                              ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(keys.trace_sha256(trace("OK")), trace_sha)
        self.assertEqual(first, [self.jobs[0].key, "control", "one", "0", "2.1.316", trace_sha,
                                 "pass"])
        self.assertEqual(second, [self.jobs[1].key, "control", "one", "1", "-", "-", "missing"])
        self.assertNotIn("stale-key", text)  # only the current configuration's keys

    def test_a_stored_run_without_a_verdict_is_unjudged(self) -> None:
        self.write("s.toml", SUITE.replace('{ regex = "OK" }', '{ judge = "says hello" }'))
        self.suite = load_suite(self.suite_path)
        self.jobs = plan.jobs(self.suite)
        self.store.put(Record(self.jobs[0].key, "control", "one", 0, "2.1.316", trace("OK")))
        self.assertTrue(self.manifest().splitlines()[2].endswith("\tunjudged"))

    def test_score_writes_the_manifest_and_never_replaces_one(self) -> None:
        out = self.root / "reports" / "keys.txt"
        code, stdout, _ = run_cli("score", str(self.suite_path), "--keys", str(out))
        self.assertEqual(code, 0, stdout)
        self.assertEqual(out.read_text(), self.manifest())
        self.assertIn(str(out), stdout)
        code, _, err = run_cli("score", str(self.suite_path), "--keys", str(out))
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
