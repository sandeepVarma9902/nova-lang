/* Nova playground: real Pyodide attempt with bulletproof graceful fallback.
 * Uses window.NovaLive (js/nova-live.js, loaded before this script) to run the
 * ACTUAL Nova interpreter (vendored nova_lang/*.py) via Pyodide from CDN.
 * On ANY failure (file:// protocol, offline, CDN blocked, interpreter
 * incompatibility) it degrades to static snippets with copy buttons and a
 * roadmap note — never a broken playground. */
(function () {
  "use strict";

  var EXAMPLES_URL = "js/examples.json";

  var examples = [];
  var current = 0;
  var live = false;

  var $ = function (id) { return document.getElementById(id); };
  var statusEl, editorEl, outputEl, runBtn;

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
      var parsed = window.NovaLive.run(src);
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

  function tryPyodide() {
    if (!window.NovaLive) {
      staticMode("interpreter loader missing.");
      return;
    }
    // fast path: file:// can never fetch the sources
    if (location.protocol === "file:") {
      staticMode("page opened from file://.");
      return;
    }
    setStatus("Loading the real Nova interpreter (Pyodide)&hellip;");
    window.NovaLive.ensure().then(function (ok) {
      if (ok) {
        live = true;
        runBtn.disabled = false;
        setStatus("&#9679; <strong>Live</strong> &mdash; running the real Nova interpreter in your browser.", "ok");
        renderOutput();
      } else {
        staticMode("could not start the in-browser interpreter (" +
          String(window.NovaLive.lastError || "unknown").replace(/</g, "&lt;") + ").");
      }
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
