/* Nova site: nav toggle, copy buttons, tiny Nova syntax highlighter. No dependencies. */
(function () {
  "use strict";

  /* mobile nav */
  var toggle = document.querySelector(".nav-toggle");
  var links = document.querySelector(".nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", function () { links.classList.toggle("open"); });
  }

  /* copy buttons for every code block */
  document.querySelectorAll("pre").forEach(function (pre) {
    if (pre.querySelector(".copy-btn")) return;
    var wrap = pre.parentElement;
    if (!wrap || !wrap.classList.contains("code-wrap")) {
      wrap = document.createElement("div");
      wrap.className = "code-wrap";
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
    }
    var btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.addEventListener("click", function () {
      var code = pre.querySelector("code");
      var text = (code ? code.innerText : pre.innerText);
      navigator.clipboard.writeText(text).then(function () {
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      });
    });
    wrap.appendChild(btn);
  });

  /* minimal Nova highlighter */

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* Single-pass tokenizer: one regex walk over the ORIGINAL source, so
   * inserted <span> markup can never be re-matched (notably the keyword
   * `class` must not match the class= attribute we just emitted). */
  var HIGHLIGHT_RE = /("(?:[^"\\]|\\.)*"|\/\/[^\n]*)|\b(\d+(?:\.\d+)?)\b|\b(Int|Float|String|Bool|Unit|List|Option|Result|Task|Stream|Subscription|Message|Map|Set)\b(?!\s*\()|\b(spawn_async|let|var|fn|effect|class|interface|extends|implements|data|init|this|super|component|state|view|import|if|else|while|for|in|match|return|requires|ensures|result|and|or|not|true|false|Some|None|Ok|Err|override|ai|spawn|nursery|context|provide|consume|app|resource|secret|env|docker|deploy)\b|\b([A-Za-z_][A-Za-z0-9_]*)(?=\s*\()|(<)(\/?)([A-Za-z][A-Za-z0-9]*)/g;

  function highlight(src) {
    var out = "", last = 0, m;
    HIGHLIGHT_RE.lastIndex = 0;
    while ((m = HIGHLIGHT_RE.exec(src)) !== null) {
      out += esc(src.slice(last, m.index));
      var tok = m[0], cls = null, html = null;
      if (m[1] !== undefined) {
        cls = (tok.indexOf("//") === 0) ? "tok-com" : "tok-str";
        html = '<span class="' + cls + '">' + esc(tok) + "</span>";
      } else if (m[2] !== undefined) {
        html = '<span class="tok-num">' + esc(tok) + "</span>";
      } else if (m[3] !== undefined) {
        html = '<span class="tok-type">' + esc(tok) + "</span>";
      } else if (m[4] !== undefined) {
        html = '<span class="tok-kw">' + esc(tok) + "</span>";
      } else if (m[5] !== undefined) {
        html = '<span class="tok-fn">' + esc(tok) + "</span>";
      } else if (m[6] !== undefined) {
        html = "&lt;" + m[7] + '<span class="tok-tag">' + esc(m[8]) + "</span>";
      }
      out += html;
      last = m.index + tok.length;
      if (tok.length === 0) { HIGHLIGHT_RE.lastIndex++; }
    }
    out += esc(src.slice(last));
    return out;
  }

  document.querySelectorAll("pre code.language-nova").forEach(function (el) {
    el.innerHTML = highlight(el.textContent);
  });

  /* shared with runnable.js: the same Nova highlighter for live widgets */
  window.NovaHighlight = highlight;
})();
