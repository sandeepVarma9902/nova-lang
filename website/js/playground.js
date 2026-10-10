/* Nova playground: real Pyodide attempt with bulletproof graceful fallback.
 * Tries to load the ACTUAL Nova interpreter (vendored nova_lang/*.py) via
 * Pyodide from CDN. On ANY failure (file:// protocol, offline, CDN blocked,
 * interpreter incompatibility) it degrades to static snippets with copy
 * buttons and a roadmap note — never a broken playground. */
(function () {
  "use strict";

  var EXAMPLES_URL = "js/examples.json";
  var PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.mjs";
  var TIMEOUT_MS = 25000;

  var examples = [];
  var current = 0;
  var pyodide = null;
  var live = false;

  var $ = function (id) { return document.getElementById(id); };
  var statusEl, editorEl, outputEl, runBtn;

  var HARNESS = [
    "import json",
    "from nova_lang.checker import check_source",
    "from nova_lang.lexer import Lexer",
    "from nova_lang.parser import Parser",
    "from nova_lang.interpreter import Interpreter, NovaError",
    "",
    "def nova_run(source):",
    "    try:",
    "        _, diags = check_source(source, '<playground>', '/tmp', is_entry=True)",
    "    except Exception as e:",
    "        return json.dumps({'ok': False, 'error': 'checker crashed: %s' % e})",
    "    if diags:",
    "        msgs = []",
    "        for d in (x.to_dict() for x in diags):",
    "            msgs.append('%s (line %s): %s' % (d.get('code'), d.get('line'), d.get('message')))",
    "        return json.dumps({'ok': False, 'error': '\\n'.join(msgs)})",
    "    try:",
    "        toks = Lexer(source, '<playground>').tokenize()",
    "        prog = Parser(toks, source, '<playground>').parse_program()",
    "        interp = Interpreter('/tmp', is_entry=True, threading='single')",
    "        out = interp.run(prog, '<playground>')",
    "        return json.dumps({'ok': True, 'output': '\\n'.join(out)})",
    "    except NovaError as e:",
    "        return json.dumps({'ok': False, 'error': '%s (line %s): %s' % (e.code, e.line, e)})",
    "    except Exception as e:",
    "        return json.dumps({'ok': False, 'error': 'runtime crashed: %s: %s' % (type(e).__name__, e)})",
    ""
  ].join("\n");

  function setStatus(html, cls) {
    statusEl.innerHTML = '<span class="' + (cls || "") + '">' + html + "</span>";
  }

  function staticMode(reason) {
    live = false;
    runBtn.disabled = true;
    runBtn.textContent = "Run (offline)";
    setStatus("&#9679; Static mode &mdash; " + reason +
      " The interactive playground needs this site served over HTTP with network access to the Pyodide CDN." +
      " Expected output is shown below; run it for real with <code>./nova run</code>. <em>Full in-browser execution is on the roadmap.</em>", "warn");
    renderOutput();
  }

  function renderOutput() {
    var ex = examples[current];
    if (!ex) return;
    if (live) {
      outputEl.innerHTML = "<em>Press Run to execute.</em>";
    } else {
      outputEl.textContent = ex.expected || "(no expected output recorded)";
    }
  }

  function selectExample(i) {
    current = i;
    editorEl.value = examples[i].source;
    document.querySelectorAll(".pg-examples button").forEach(function (b, j) {
      b.classList.toggle("active", j === i);
    });
    renderOutput();
  }

  function runLive() {
    var src = editorEl.value;
    outputEl.innerHTML = "<em>Running&hellip;</em>";
    try {
      pyodide.globals.set("__nova_src", src);
      var res = pyodide.runPython("nova_run(__nova_src)");
      var parsed = JSON.parse(res);
      if (parsed.ok) {
        outputEl.textContent = parsed.output;
      } else {
        outputEl.innerHTML = '<span style="color:#f47c7c">' +
          parsed.error.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</span>";
      }
    } catch (e) {
      outputEl.innerHTML = '<span style="color:#f47c7c">interpreter error: ' +
        String(e).replace(/</g, "&lt;") + "</span>";
    }
  }

  function withTimeout(promise, ms, label) {
    return Promise.race([
      promise,
      new Promise(function (_, reject) {
        setTimeout(function () { reject(new Error(label + " timed out")); }, ms);
      })
    ]);
  }

  function tryPyodide() {
    // fast path: file:// can never fetch the sources
    if (location.protocol === "file:") {
      staticMode("page opened from file://.");
      return;
    }
    setStatus("Loading the real Nova interpreter (Pyodide)&hellip;");
    withTimeout(import(PYODIDE_URL), TIMEOUT_MS, "CDN load")
      .then(function (mod) { return withTimeout(mod.loadPyodide(), TIMEOUT_MS, "Pyodide init"); })
      .then(function (pd) {
        pyodide = pd;
        return withTimeout(fetch("nova_py/files.json").then(function (r) {
          if (!r.ok) throw new Error("source manifest missing");
          return r.json();
        }), TIMEOUT_MS, "source manifest");
      })
      .then(function (files) {
        var loads = files.filter(function (f) { return f !== "files.json"; })
          .map(function (f) {
            return fetch("nova_py/" + f).then(function (r) {
              if (!r.ok) throw new Error("missing " + f);
              return r.text();
            }).then(function (text) {
              pyodide.FS.writeFile("/nova_py/" + f, text);
            });
          });
        return withTimeout(Promise.all(loads), TIMEOUT_MS, "source load");
      })
      .then(function () {
        pyodide.runPython("import sys; sys.path.insert(0, '/nova_py')\n" + HARNESS);
        // smoke test: the interpreter must actually execute
        pyodide.globals.set("__nova_src", 'print("nova-ok")');
        var res = JSON.parse(pyodide.runPython("nova_run(__nova_src)"));
        if (!res.ok || res.output.trim() !== "nova-ok") throw new Error("smoke test failed");
        live = true;
        runBtn.disabled = false;
        setStatus("&#9679; <strong>Live</strong> &mdash; running the real Nova interpreter in your browser.", "ok");
        renderOutput();
      })
      .catch(function (e) {
        staticMode("could not start the in-browser interpreter (" +
          String(e.message || e).replace(/</g, "&lt;") + ").");
      });
  }

  function init() {
    statusEl = $("pg-status");
    editorEl = $("pg-editor");
    outputEl = $("pg-output");
    runBtn = $("pg-run-btn");
    fetch(EXAMPLES_URL).then(function (r) { return r.json(); })
      .then(function (data) {
        examples = data;
        var list = $("pg-examples");
        examples.forEach(function (ex, i) {
          var b = document.createElement("button");
          b.innerHTML = "<strong>" + ex.title + "</strong><small>" + ex.desc + "</small>";
          b.addEventListener("click", function () { selectExample(i); });
          list.appendChild(b);
        });
        selectExample(0);
        runBtn.addEventListener("click", function () { if (live) runLive(); });
        tryPyodide();
      })
      .catch(function () {
        statusEl.innerHTML = "Could not load examples.";
      });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
