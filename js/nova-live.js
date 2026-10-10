/* window.NovaLive — shared in-browser Nova interpreter loader.
 * Plain classic script (no modules). Exposes:
 *   window.NovaLive.ensure() -> Promise<boolean>  (true = live interpreter ready)
 *   window.NovaLive.run(source) -> {ok:true,output} | {ok:false,error}  (sync; call after ensure() is true)
 *   window.NovaLive.lastError -> last load failure message (for status text)
 * Loads the REAL Nova interpreter (vendored nova_lang/*.py) via Pyodide
 * from the pinned CDN. ensure() loads exactly once (cached promise) and
 * resolves false on ANY failure (file://, offline, CDN blocked) — never throws.
 * No DOM manipulation in this file. */
(function () {
  "use strict";

  var TIMEOUT_MS = 25000;

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

  var pyodide = null;
  var readyPromise = null;

  function withTimeout(promise, ms, label) {
    return Promise.race([
      promise,
      new Promise(function (_, reject) {
        setTimeout(function () { reject(new Error(label + " timed out")); }, ms);
      })
    ]);
  }

  function novaPyBase() {
    // nova-live.js lives in <site>/js/ ; the vendored sources live in <site>/nova_py/
    var scripts = document.getElementsByTagName("script");
    for (var i = scripts.length - 1; i >= 0; i--) {
      var s = scripts[i].getAttribute("src") || "";
      if (s.indexOf("nova-live.js") !== -1) {
        return new URL("../nova_py/", new URL(s, document.baseURI)).toString();
      }
    }
    return new URL("../nova_py/", new URL("js/nova-live.js", document.baseURI)).toString();
  }

  var CDNS = [
    { mjs: "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.mjs",
      idx: "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/" },
    { mjs: "https://unpkg.com/pyodide@0.26.4/pyodide.mjs",
      idx: "https://unpkg.com/pyodide@0.26.4/" }
  ];

  function tryCdn(i) {
    if (i >= CDNS.length) return Promise.reject(new Error("Pyodide CDN unreachable"));
    var cdn = CDNS[i];
    return withTimeout(import(cdn.mjs), TIMEOUT_MS, "CDN load")
      .then(function (mod) {
        return withTimeout(mod.loadPyodide({ indexURL: cdn.idx }), TIMEOUT_MS, "Pyodide init");
      })
      .catch(function () { return tryCdn(i + 1); });
  }

  function load() {
    // fast path: file:// can never fetch the sources
    if (location.protocol === "file:") {
      window.NovaLive.lastError = "page opened from file://";
      return Promise.resolve(false);
    }
    var base = novaPyBase();
    return tryCdn(0)
      .then(function (pd) {
        pyodide = pd;
        return withTimeout(fetch(base + "files.json").then(function (r) {
          if (!r.ok) throw new Error("source manifest missing");
          return r.json();
        }), TIMEOUT_MS, "source manifest");
      })
      .then(function (files) {
        // Recreate the real package layout: /nova_py/nova_lang/*.py, so
        // `from nova_lang.checker import ...` and relative imports work.
        try { pyodide.FS.mkdir("/nova_py"); } catch (e) { /* exists */ }
        try { pyodide.FS.mkdir("/nova_py/nova_lang"); } catch (e) { /* exists */ }
        var loads = files.filter(function (f) { return f !== "files.json"; })
          .map(function (f) {
            return fetch(base + f).then(function (r) {
              if (!r.ok) throw new Error("missing " + f);
              return r.text();
            }).then(function (text) {
              pyodide.FS.writeFile("/nova_py/nova_lang/" + f, text);
            });
          });
        return withTimeout(Promise.all(loads), TIMEOUT_MS, "source load");
      })
      .then(function () {
        try {
          pyodide.runPython("import sys; sys.path.insert(0, '/nova_py')\n" + HARNESS);
        } catch (e3) {
          throw new Error("interpreter import failed: " + String((e3 && e3.message) || e3));
        }
        // smoke test: the interpreter must actually execute
        pyodide.globals.set("__nova_src", 'print("nova-ok")');
        var res = JSON.parse(pyodide.runPython("nova_run(__nova_src)"));
        if (!res.ok || res.output.trim() !== "nova-ok") throw new Error("smoke test failed: " + JSON.stringify(res).slice(0, 160));
        return true;
      })
      .catch(function (e) {
        pyodide = null;
        readyPromise = null; // allow a later Run click to retry
        window.NovaLive.lastError = String((e && e.message) || e);
        return false;
      });
  }

  window.NovaLive = {
    lastError: "",
    ensure: function () {
      if (!readyPromise) readyPromise = load();
      return readyPromise;
    },
    run: function (source) {
      // synchronous; only call after ensure() resolved true
      pyodide.globals.set("__nova_src", source);
      return JSON.parse(pyodide.runPython("nova_run(__nova_src)"));
    }
  };
})();
