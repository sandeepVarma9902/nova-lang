/* runnable.js — turns every `pre code.language-nova` block into an
 * interactive widget: syntax-highlighted read view, "Edit & run" toggle,
 * editable textarea, Run/Reset, and a live output pane powered by
 * window.NovaLive (the real Nova interpreter via Pyodide).
 * Real compiler diagnostics are shown as-is — they teach.
 * Snippets containing `serve {` never get a Run button (a server would hang
 * the page); they get a note + copy button instead. If Pyodide can't load
 * (file://, offline, CDN blocked) the widget still renders readable,
 * copyable code and the Run button explains itself via tooltip. */
(function () {
  "use strict";

  var OFFLINE_HINT = "live run needs network — try the Playground";

  function offlineReason() {
    var err = window.NovaLive && window.NovaLive.lastError;
    return OFFLINE_HINT + (err ? " (" + err + ")" : "");
  }

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function highlight(src) {
    if (window.NovaHighlight) return window.NovaHighlight(src);
    return esc(src);
  }

  function isServerSnippet(src) {
    // serve { ... } (block form) and serve(r, port, cap) (call form) both
    // block forever — never offer live run for them
    return /\bserve\s*[\{\(]/.test(src);
  }

  function copyText(btn, text) {
    function done() {
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1500);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, done);
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
      done();
    }
  }

  function el(tag, cls, html) {
    var d = document.createElement(tag);
    if (cls) d.className = cls;
    if (html !== undefined) d.innerHTML = html;
    return d;
  }

  function buildWidget(pre, code) {
    var source = code.textContent.replace(/\s+$/, "");
    var serverOnly = isServerSnippet(source);
    var liveAvailable = !!window.NovaLive;

    var widget = el("div", "nv-run");

    /* toolbar */
    var bar = el("div", "nv-run-bar");
    bar.appendChild(el("span", "nv-run-tag", "nova"));
    var copyBtn = el("button", "nv-run-copy", "Copy");
    copyBtn.type = "button";
    copyBtn.addEventListener("click", function () { copyText(copyBtn, source); });
    bar.appendChild(copyBtn);

    var toggleBtn = null;
    if (!serverOnly) {
      toggleBtn = el("button", "nv-run-toggle", "Edit &amp; run");
      toggleBtn.type = "button";
      bar.appendChild(toggleBtn);
    } else {
      var note = el("span", "nv-run-note",
        "starts a server &mdash; run locally with <code>./nova run</code>");
      bar.appendChild(note);
    }
    widget.appendChild(bar);

    /* read view */
    var read = el("pre", "nv-run-read");
    var readCode = el("code", "language-nova", highlight(source));
    read.appendChild(readCode);
    widget.appendChild(read);

    if (!serverOnly) {
      /* edit view */
      var edit = el("div", "nv-run-edit");
      edit.hidden = true;
      var ta = el("textarea", "nv-run-editor");
      ta.value = source;
      ta.spellcheck = false;
      ta.setAttribute("rows", String(Math.min(28, Math.max(6, source.split("\n").length + 1))));
      edit.appendChild(ta);

      var actions = el("div", "nv-run-actions");
      var runBtn = el("button", "nv-run-btn", "Run");
      runBtn.type = "button";
      var resetBtn = el("button", "nv-run-reset", "Reset");
      resetBtn.type = "button";
      var status = el("span", "nv-run-status");
      actions.appendChild(runBtn);
      actions.appendChild(resetBtn);
      actions.appendChild(status);
      edit.appendChild(actions);

      var out = el("pre", "nv-run-output");
      out.hidden = true;
      edit.appendChild(out);
      widget.appendChild(edit);

      var editing = false;
      toggleBtn.addEventListener("click", function () {
        editing = !editing;
        read.hidden = editing;
        edit.hidden = !editing;
        toggleBtn.innerHTML = editing ? "Done" : "Edit &amp; run";
        if (editing) ta.focus();
      });
      resetBtn.addEventListener("click", function () {
        ta.value = source;
        out.hidden = true;
        out.textContent = "";
        status.textContent = "";
      });

      runBtn.addEventListener("click", function () {
        if (!liveAvailable) {
          status.textContent = offlineReason();
          return;
        }
        runBtn.disabled = true;
        out.hidden = true;
        status.textContent = "loading interpreter\u2026";
        window.NovaLive.ensure().then(function (ok) {
          if (!ok) {
            status.textContent = offlineReason();
            runBtn.title = offlineReason();
            runBtn.disabled = false;
            return;
          }
          status.textContent = "running\u2026";
          var res;
          try {
            res = window.NovaLive.run(ta.value);
          } catch (e) {
            res = { ok: false, error: "interpreter error: " + String(e) };
          }
          out.hidden = false;
          out.textContent = res.ok ? res.output : res.error;
          out.classList.toggle("nv-err", !res.ok);
          status.textContent = "";
          runBtn.disabled = false;
        });
      });
      if (!liveAvailable) runBtn.title = offlineReason();
    }

    /* swap into the page: replace the .code-wrap main.js added (or the pre) */
    var host = (pre.parentElement && pre.parentElement.classList.contains("code-wrap"))
      ? pre.parentElement : pre;
    host.parentNode.replaceChild(widget, host);
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("pre code.language-nova").forEach(function (code) {
      var pre = code.closest("pre");
      if (!pre || pre.dataset.nvDone) return;
      pre.dataset.nvDone = "1";
      try {
        buildWidget(pre, code);
      } catch (e) {
        /* never break the docs page: leave the static block in place */
        if (window.console) console.warn("runnable widget failed:", e);
      }
    });
  });
})();
