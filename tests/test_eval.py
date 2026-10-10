#!/usr/bin/env python3
# test_eval.py — tests for nova_lang/evalharness.py (`nova eval`).
#
# Everything here is offline: the end-to-end runs use a canned stub
# provider with deterministic outputs, plus the real MockProvider (which
# never touches the network). No real API is ever hit.
#
# Run:  python3 tests/test_eval.py        (from the nova-lang directory)

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nova_lang.evalharness import (
    EvalError, evaluate_predicate, load_suite, run_eval, run_suite,
)
from nova_lang.providers import AIProvider


class CannedProvider(AIProvider):
    """Deterministic stub: prompt -> canned completion text."""
    name = "canned"

    def __init__(self, outputs):
        self.outputs = outputs
        self.seen_prompts = []

    def complete(self, request):
        self.seen_prompts.append(request.prompt)
        return self.outputs.get(request.prompt, "")


def make_suite(evals, **kwargs):
    suite = {"suite": "test-suite", "evals": evals}
    suite.update(kwargs)
    return suite


def write_suite_file(suite_obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(suite_obj, fh)
    return path


# ------------------------------------------------------- predicate units ---

class PredicateTest(unittest.TestCase):
    def check(self, pred, output):
        return evaluate_predicate(pred, output)

    def test_contains(self):
        ok, _ = self.check({"contains": "foo"}, "say foo now")
        self.assertTrue(ok)
        ok, detail = self.check({"contains": "foo"}, "say bar now")
        self.assertFalse(ok)
        self.assertIn("foo", detail)

    def test_not_contains(self):
        ok, _ = self.check({"not_contains": "foo"}, "say bar now")
        self.assertTrue(ok)
        ok, _ = self.check({"not_contains": "foo"}, "say foo now")
        self.assertFalse(ok)

    def test_regex(self):
        ok, _ = self.check({"regex": r"\d+ cats"}, "i have 12 cats")
        self.assertTrue(ok)
        ok, _ = self.check({"regex": r"^\d+$"}, "12 cats")
        self.assertFalse(ok)

    def test_equals(self):
        ok, _ = self.check({"equals": "exact"}, "exact")
        self.assertTrue(ok)
        ok, _ = self.check({"equals": "exact"}, "exact ")
        self.assertFalse(ok)

    def test_json_valid(self):
        ok, _ = self.check({"json_valid": True}, '{"a": 1}')
        self.assertTrue(ok)
        ok, detail = self.check({"json_valid": True}, "not json")
        self.assertFalse(ok)
        self.assertIn("not valid JSON", detail)
        # bare-string predicate form
        ok, _ = self.check({"json_valid": True}, "[1, 2]")
        self.assertTrue(ok)

    def test_json_has_key(self):
        ok, _ = self.check({"json_has_key": "a"}, '{"a": 1, "b": 2}')
        self.assertTrue(ok)
        ok, detail = self.check({"json_has_key": "zzz"}, '{"a": 1}')
        self.assertFalse(ok)
        self.assertIn("zzz", detail)
        # dotted path into nested objects
        ok, _ = self.check({"json_has_key": "a.b"},
                            '{"a": {"b": 3}}')
        self.assertTrue(ok)
        ok, _ = self.check({"json_has_key": "a.b.c"},
                            '{"a": {"b": 3}}')
        self.assertFalse(ok)
        # invalid JSON fails the predicate rather than raising
        ok, _ = self.check({"json_has_key": "a"}, "nope")
        self.assertFalse(ok)

    def test_detail_strings_are_human_readable(self):
        for pred, out in [({"contains": "x"}, "x"),
                          ({"regex": "x"}, "x"),
                          ({"equals": "x"}, "x"),
                          ({"json_valid": True}, "{}")]:
            ok, detail = self.check(pred, out)
            self.assertTrue(ok)
            self.assertIsInstance(detail, str)
            self.assertTrue(detail)


# ------------------------------------------------------------ suite load ---

class LoadSuiteTest(unittest.TestCase):
    def test_valid_suite(self):
        path = write_suite_file(make_suite([
            {"name": "a", "prompt": "hi", "predicate": {"contains": "h"}},
        ]))
        try:
            suite = load_suite(path)
            self.assertEqual(suite["evals"][0]["predicate"],
                             {"contains": "h"})
        finally:
            os.unlink(path)

    def test_bare_string_predicate_normalized(self):
        path = write_suite_file(make_suite([
            {"name": "a", "prompt": "hi", "predicate": "json_valid"},
        ]))
        try:
            suite = load_suite(path)
            self.assertEqual(suite["evals"][0]["predicate"],
                             {"json_valid": True})
        finally:
            os.unlink(path)

    def test_missing_file(self):
        with self.assertRaises(EvalError):
            load_suite("/tmp/does-not-exist-nova-eval.json")

    def test_not_json(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write("{not json")
        try:
            with self.assertRaises(EvalError):
                load_suite(path)
        finally:
            os.unlink(path)

    def test_missing_evals(self):
        path = write_suite_file({"suite": "empty"})
        try:
            with self.assertRaises(EvalError):
                load_suite(path)
        finally:
            os.unlink(path)

    def test_duplicate_names(self):
        path = write_suite_file(make_suite([
            {"name": "dup", "prompt": "a", "predicate": {"contains": "a"}},
            {"name": "dup", "prompt": "b", "predicate": {"contains": "b"}},
        ]))
        try:
            with self.assertRaises(EvalError):
                load_suite(path)
        finally:
            os.unlink(path)

    def test_unknown_predicate(self):
        path = write_suite_file(make_suite([
            {"name": "a", "prompt": "hi",
             "predicate": {"vibes": "good"}},
        ]))
        try:
            with self.assertRaises(EvalError):
                load_suite(path)
        finally:
            os.unlink(path)

    def test_bad_regex_rejected_at_load(self):
        path = write_suite_file(make_suite([
            {"name": "a", "prompt": "hi",
             "predicate": {"regex": "(unclosed"}},
        ]))
        try:
            with self.assertRaises(EvalError):
                load_suite(path)
        finally:
            os.unlink(path)

    def test_missing_prompt(self):
        path = write_suite_file(make_suite([
            {"name": "a", "predicate": {"contains": "a"}},
        ]))
        try:
            with self.assertRaises(EvalError):
                load_suite(path)
        finally:
            os.unlink(path)


# ------------------------------------------------------ end-to-end: stub ---

class RunSuiteStubTest(unittest.TestCase):
    def test_report_shape_and_pass_fail(self):
        suite = make_suite([
            {"name": "pass-one", "prompt": "p1",
             "predicate": {"contains": "hello"}},
            {"name": "fail-one", "prompt": "p2",
             "predicate": {"equals": "exact-match"}},
            {"name": "json-one", "prompt": "p3",
             "predicate": {"json_has_key": "answer"}},
        ])
        provider = CannedProvider({
            "p1": "well hello there",
            "p2": "close but not exact",
            "p3": '{"answer": 42}',
        })
        report = run_suite(suite, provider=provider)
        self.assertEqual(report["suite"], "test-suite")
        self.assertEqual(report["provider"], "canned")
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["failed"], 1)
        by_name = {e["name"]: e for e in report["evals"]}
        for ev in report["evals"]:
            for key in ("name", "pass", "latency_ms", "output_excerpt",
                        "predicate"):
                self.assertIn(key, ev)
            self.assertIsInstance(ev["latency_ms"], float)
            self.assertGreaterEqual(ev["latency_ms"], 0.0)
        self.assertTrue(by_name["pass-one"]["pass"])
        self.assertFalse(by_name["fail-one"]["pass"])
        self.assertTrue(by_name["json-one"]["pass"])
        self.assertIn("hello", by_name["pass-one"]["output_excerpt"])
        # every prompt really went through the stub provider
        self.assertEqual(sorted(provider.seen_prompts), ["p1", "p2", "p3"])

    def test_prompt_rendered_through_nova_ai_fn(self):
        # Quotes, newlines and tabs must survive the generated Nova source.
        prompt = 'Say "hi"\nthen\ttab.'
        suite = make_suite([
            {"name": "esc", "prompt": prompt,
             "predicate": {"contains": "hi"}},
        ])
        provider = CannedProvider({prompt: "hi back"})
        report = run_suite(suite, provider=provider)
        self.assertTrue(report["evals"][0]["pass"])
        self.assertEqual(provider.seen_prompts, [prompt])

    def test_suite_level_defaults_apply(self):
        suite = make_suite(
            [{"name": "a", "prompt": "p", "predicate": {"contains": "x"}}],
            model="my-model", temperature=0.1, max_tokens=32)
        provider = CannedProvider({"p": "x"})
        report = run_suite(suite, provider=provider)
        self.assertTrue(report["evals"][0]["pass"])

    def test_unknown_provider_name_fails_fast(self):
        suite = make_suite([
            {"name": "a", "prompt": "p", "predicate": {"contains": "x"}},
        ])
        with self.assertRaises(EvalError):
            run_suite(suite, provider_override="no-such-provider")


# ------------------------------------------------------ end-to-end: mock ---

class RunSuiteMockTest(unittest.TestCase):
    def test_mock_provider_end_to_end(self):
        suite = make_suite([
            {"name": "mock-tag", "prompt": "Summarize Nova.",
             "predicate": {"contains": "[mock]"}},
            {"name": "mock-echoes", "prompt": "hello there",
             "predicate": {"contains": "hello there"}},
        ])
        report = run_suite(suite, provider_override="mock")
        self.assertEqual(report["provider"], "mock")
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["failed"], 0)

    def test_env_provider_selection_honored(self):
        old = os.environ.get("NOVA_AI_PROVIDER")
        os.environ["NOVA_AI_PROVIDER"] = "mock"
        try:
            suite = make_suite([
                {"name": "a", "prompt": "hi",
                 "predicate": {"contains": "[mock]"}},
            ])
            report = run_suite(suite)  # no override -> env wins
            self.assertEqual(report["provider"], "mock")
            self.assertEqual(report["passed"], 1)
        finally:
            if old is None:
                del os.environ["NOVA_AI_PROVIDER"]
            else:
                os.environ["NOVA_AI_PROVIDER"] = old


# ---------------------------------------------------------------- run_eval ---

class RunEvalExitCodeTest(unittest.TestCase):
    def _run(self, suite_obj, **kwargs):
        path = write_suite_file(suite_obj)
        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = run_eval(path, **kwargs)
        finally:
            os.unlink(path)
        return rc, out.getvalue(), err.getvalue()

    def test_all_pass_exit_zero_and_stdout_is_json(self):
        suite = make_suite([
            {"name": "a", "prompt": "p1", "predicate": {"contains": "yes"}},
        ])
        # stub can't be passed through run_eval's CLI signature, so use
        # the mock provider's deterministic "[mock] ..." output instead.
        suite["evals"][0]["prompt"] = "yes indeed"
        suite["evals"][0]["predicate"] = {"contains": "[mock]"}
        rc, stdout, stderr = self._run(suite,
                                       provider_override="mock")
        self.assertEqual(rc, 0)
        report = json.loads(stdout)  # stdout is pure JSON
        self.assertEqual(report["passed"], 1)
        self.assertIn("1/1 passed", stderr)

    def test_failure_exit_one(self):
        suite = make_suite([
            {"name": "a", "prompt": "whatever",
             "predicate": {"contains": "zzz-never-there"}},
        ])
        rc, stdout, stderr = self._run(suite,
                                       provider_override="mock")
        self.assertEqual(rc, 1)
        report = json.loads(stdout)
        self.assertEqual(report["failed"], 1)
        self.assertIn("failed: a", stderr)

    def test_bad_suite_exit_two(self):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = run_eval("/tmp/does-not-exist-nova-eval.json")
        self.assertEqual(rc, 2)
        self.assertIn("nova eval:", err.getvalue())

    def test_out_path_writes_report_file(self):
        suite = make_suite([
            {"name": "a", "prompt": "hi",
             "predicate": {"contains": "[mock]"}},
        ])
        path = write_suite_file(suite)
        fd, out_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = run_eval(path, provider_override="mock",
                              out_path=out_path)
            self.assertEqual(rc, 0)
            with open(out_path) as fh:
                saved = json.load(fh)
            self.assertEqual(saved, json.loads(out.getvalue()))
        finally:
            os.unlink(path)
            os.unlink(out_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
