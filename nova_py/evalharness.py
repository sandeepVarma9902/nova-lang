# evalharness.py — `nova eval`: eval-driven LLM iteration for Nova.
#
# This is the honest v1 of "training things": it does NOT train, fine-tune,
# or otherwise modify any model. It runs a suite of prompts through a Nova
# `ai fn` (mock provider by default; the configured provider via
# providers.select_provider when requested), checks each completion against
# a declared predicate, and reports pass/fail + latency as JSON.
#
# The intended loop is: tweak the prompt (or model/config), re-run the
# suite, diff the report. Improvement comes from iteration, not from
# anything this harness does to the model.
#
# Stdlib only.

import json
import os
import re
import sys
import time

from .checker import check_source
from .interpreter import (Interpreter, AiFuncValue, NovaError, nova_repr)
from .lexer import Lexer
from .parser import Parser
from .providers import AIRequest, select_provider


class EvalError(Exception):
    """A problem with the suite file itself (not an eval failure)."""


# ---------------------------------------------------------------- suite ---

PREDICATES = ("contains", "not_contains", "regex", "equals",
              "json_valid", "json_has_key")


def _is_str(x):
    return isinstance(x, str)


def load_suite(path):
    """Load and validate a JSON eval suite. Returns the suite dict.

    Raises EvalError on any structural problem."""
    if not os.path.isfile(path):
        raise EvalError("suite file not found: %s" % path)
    try:
        with open(path) as fh:
            suite = json.load(fh)
    except (OSError, ValueError) as e:
        raise EvalError("suite file is not valid JSON: %s" % e)
    if not isinstance(suite, dict):
        raise EvalError("suite must be a JSON object")
    evals = suite.get("evals")
    if not isinstance(evals, list) or not evals:
        raise EvalError('suite must contain a non-empty "evals" list')
    seen = set()
    for i, ev in enumerate(evals):
        where = "evals[%d]" % i
        if not isinstance(ev, dict):
            raise EvalError("%s must be an object" % where)
        name = ev.get("name")
        if not _is_str(name) or not name:
            raise EvalError("%s needs a non-empty string \"name\"" % where)
        if name in seen:
            raise EvalError("duplicate eval name: %r" % name)
        seen.add(name)
        if not _is_str(ev.get("prompt")):
            raise EvalError("eval %r needs a string \"prompt\"" % name)
        for key in ("model", "temperature", "max_tokens"):
            if key in ev and not isinstance(ev[key], (str, int, float)):
                raise EvalError(
                    "eval %r: %r must be a string/number" % (name, key))
        ev["predicate"] = _validate_predicate(ev.get("predicate"), name)
    for key in ("model", "temperature", "max_tokens"):
        if key in suite and not isinstance(suite[key], (str, int, float)):
            raise EvalError("suite-level %r must be a string/number" % key)
    return suite


def _validate_predicate(pred, name):
    """Normalize a predicate spec to {"<kind>": <arg>}."""
    # Bare string form for the arg-less predicate: "json_valid".
    if _is_str(pred) and pred in PREDICATES:
        pred = {pred: True}
    if not isinstance(pred, dict) or len(pred) != 1:
        raise EvalError(
            "eval %r: predicate must be like {\"contains\": \"...\"} "
            "(one of: %s)" % (name, ", ".join(PREDICATES)))
    kind, arg = next(iter(pred.items()))
    if kind not in PREDICATES:
        raise EvalError("eval %r: unknown predicate %r (want one of: %s)"
                        % (name, kind, ", ".join(PREDICATES)))
    if kind in ("contains", "not_contains", "equals", "json_has_key"):
        if not _is_str(arg):
            raise EvalError("eval %r: predicate %r needs a string argument"
                            % (name, kind))
    elif kind == "regex":
        if not _is_str(arg):
            raise EvalError("eval %r: predicate \"regex\" needs a string "
                            "pattern" % name)
        try:
            re.compile(arg)
        except re.error as e:
            raise EvalError("eval %r: bad regex %r: %s" % (name, arg, e))
    elif kind == "json_valid":
        if arg not in (True,):
            raise EvalError("eval %r: predicate \"json_valid\" takes no "
                            "argument (use true)" % name)
    return {kind: arg}


# ------------------------------------------------------------- predicates ---

def evaluate_predicate(predicate, output):
    """Check one predicate against the completion text.

    Returns (passed: bool, detail: str)."""
    kind, arg = next(iter(predicate.items()))
    if kind == "contains":
        ok = arg in output
        return ok, ("output contains %r" % arg if ok
                    else "output does not contain %r" % arg)
    if kind == "not_contains":
        ok = arg not in output
        return ok, ("output avoids %r" % arg if ok
                    else "output unexpectedly contains %r" % arg)
    if kind == "regex":
        ok = re.search(arg, output) is not None
        return ok, ("output matches /%s/" % arg if ok
                    else "output does not match /%s/" % arg)
    if kind == "equals":
        ok = output == arg
        return ok, ("output equals expected text" if ok
                    else "output differs from expected text")
    if kind == "json_valid":
        try:
            json.loads(output)
        except ValueError as e:
            return False, "output is not valid JSON: %s" % e
        return True, "output is valid JSON"
    if kind == "json_has_key":
        try:
            data = json.loads(output)
        except ValueError as e:
            return False, "output is not valid JSON: %s" % e
        cur = data
        for part in arg.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return False, "JSON has no key path %r" % arg
        return True, "JSON has key path %r" % arg
    raise EvalError("unknown predicate %r" % kind)  # unreachable


# ---------------------------------------------------------------- runner ---

def _nova_escape(s):
    """Encode text as the inside of a Nova double-quoted string literal."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32:
            continue  # Nova string escapes don't cover other controls
        else:
            out.append(ch)
    return "".join(out)


def _render_ai_fn_source(name, prompt, model, temperature, max_tokens):
    """Build a tiny Nova program whose `ai fn` renders this eval's prompt.

    The prompt goes through the REAL pipeline: lex -> parse -> type-check ->
    interpreter executes the ai fn body and collects prompt(...) parts ->
    the selected provider completes it. The mock provider's completion is
    deterministic, so suites are reproducible."""
    lines = ["ai fn %s() -> String" % name]
    if model is not None:
        lines.append('  model = "%s"' % _nova_escape(str(model)))
    if temperature is not None:
        lines.append("  temperature = %s" % temperature)
    if max_tokens is not None:
        lines.append("  max_tokens = %s" % max_tokens)
    lines.append("{")
    lines.append('  prompt("%s")' % _nova_escape(prompt))
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


class _EvalInterpreter(Interpreter):
    """Interpreter with an injectable AI provider for tests.

    When `eval_provider` (an AIProvider instance) is given, the ai fn's
    provider step uses it directly; otherwise the standard
    providers.select_provider() path runs, honoring NOVA_AI_PROVIDER and
    any explicit override. The AIRequest construction mirrors
    Interpreter.provider_complete so injected and real providers see the
    same request shape."""

    def __init__(self, *args, eval_provider=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._eval_provider = eval_provider

    def provider_complete(self, decl, prompt, node):
        if self._eval_provider is None:
            return super().provider_complete(decl, prompt, node)
        schema = self._resolve_schema(decl.ret, node)
        req = AIRequest(
            model=decl.config.get("model", "nova-mock-1"),
            prompt=prompt,
            schema=schema,
            schema_json=self.schema_json(schema),
            temperature=decl.config.get("temperature", 0.7),
            max_tokens=decl.config.get("max_tokens", 512),
            stream=bool(decl.config.get("stream", False)),
            build_mock=lambda t, p: self.build_mock(t, p),
            coerce_json=lambda data, t: self.coerce_json(data, t, node),
        )
        try:
            return self._eval_provider.complete(req)
        except NovaError:
            raise
        except Exception as e:
            self.err("R_AI_PROVIDER", "AI provider failed: %s" % e, node)


def _invoke_eval(interp, fn_name, source):
    """Type-check the generated source, then call its ai fn.

    Returns the completion as plain text. Raises NovaError on any
    Nova-side failure."""
    _, diags = check_source(source, "<eval>", interp.basedir, is_entry=True)
    if diags:
        d = diags[0]
        raise NovaError(getattr(d, "code", "E_EVAL"),
                        "eval program failed to check: %s"
                        % getattr(d, "message", d),
                        getattr(d, "line", 0), getattr(d, "col", 0))
    prog = Parser(Lexer(source, "<eval>").tokenize(),
                  source, "<eval>").parse_program()
    decl = None
    for stmt in prog.stmts:
        interp.register(stmt)
        if decl is None and hasattr(stmt, "name") \
                and getattr(stmt, "name", "") == fn_name:
            decl = stmt
    if decl is None:
        raise NovaError("E_EVAL", "generated ai fn %r not found" % fn_name)
    value = interp.call_ai(AiFuncValue(decl, interp.global_env), [],
                           decl, fn_name)
    return nova_repr(value)


def _provider_name(provider, provider_override):
    if provider is not None:
        return getattr(provider, "name", type(provider).__name__)
    return (provider_override
            or os.environ.get("NOVA_AI_PROVIDER", "mock"))


def run_suite(suite, provider_override=None, provider=None):
    """Run every eval in a loaded suite. Returns the report dict.

    `provider_override`: provider name passed to select_provider
    (honors NOVA_AI_PROVIDER when None). `provider`: an AIProvider
    instance used directly (test seam; no network either way)."""
    if provider is None:
        try:
            select_provider(provider_override)
        except RuntimeError as e:
            raise EvalError(str(e))
    pname = _provider_name(provider, provider_override)
    results = []
    for i, ev in enumerate(suite["evals"]):
        name = ev["name"]
        fn_name = "__nova_eval_%d" % i
        model = ev.get("model", suite.get("model", "nova-eval"))
        temperature = ev.get("temperature", suite.get("temperature"))
        max_tokens = ev.get("max_tokens", suite.get("max_tokens"))
        source = _render_ai_fn_source(fn_name, ev["prompt"], model,
                                      temperature, max_tokens)
        interp = _EvalInterpreter(os.getcwd(), provider=provider_override,
                                  eval_provider=provider,
                                  is_entry=True, threading="single")
        t0 = time.perf_counter()
        try:
            output = _invoke_eval(interp, fn_name, source)
            passed, detail = evaluate_predicate(ev["predicate"], output)
            error = None
        except (NovaError, EvalError) as e:
            output, passed = "", False
            detail = "eval errored before the predicate ran"
            error = "%s: %s" % (getattr(e, "code", "EVAL"), e)
        except Exception as e:  # never let one eval kill the suite
            output, passed = "", False
            detail = "eval errored before the predicate ran"
            error = "EVAL: %s: %s" % (type(e).__name__, e)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        results.append({
            "name": name,
            "pass": passed,
            "latency_ms": round(latency_ms, 2),
            "output_excerpt": output[:200],
            "predicate": ev["predicate"],
            "detail": detail,
            "error": error,
        })
    passed_n = sum(1 for r in results if r["pass"])
    return {
        "suite": suite.get("suite", "unnamed"),
        "provider": pname,
        "total": len(results),
        "passed": passed_n,
        "failed": len(results) - passed_n,
        "evals": results,
    }


def _summary_line(report):
    fails = [e["name"] for e in report["evals"] if not e["pass"]]
    line = ("eval: %s — %d/%d passed (provider=%s)"
            % (report["suite"], report["passed"], report["total"],
               report["provider"]))
    if fails:
        line += "; failed: %s" % ", ".join(fails)
    return line


def run_eval(suite_path, provider_override=None, out_path=None):
    """CLI entry: `nova eval <suite.json>`. Prints the JSON report to
    stdout and a human-readable summary to stderr.

    Returns an exit code: 0 = all evals passed, 1 = at least one failed,
    2 = the suite itself could not run."""
    try:
        suite = load_suite(suite_path)
    except EvalError as e:
        sys.stderr.write("nova eval: %s\n" % e)
        return 2
    try:
        report = run_suite(suite, provider_override=provider_override)
    except EvalError as e:
        sys.stderr.write("nova eval: %s\n" % e)
        return 2
    text = json.dumps(report, indent=2)
    sys.stdout.write(text + "\n")
    if out_path:
        with open(out_path, "w") as fh:
            fh.write(text + "\n")
    sys.stderr.write(_summary_line(report) + "\n")
    return 0 if report["failed"] == 0 else 1
