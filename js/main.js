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
  var KEYWORDS = ("let var fn effect class interface extends implements data init this super " +
    "component state view import if else while for in match return requires ensures " +
    "result and or not true false Some None Ok Err override ai spawn stream async " +
    "spawn_async nursery context provide consume app resource secret env docker deploy").split(" ");
  var TYPES = ["Int", "Float", "String", "Bool", "Unit", "List", "Option", "Result",
    "Task", "Stream", "Subscription", "Message", "Map", "Set"];

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function highlight(src) {
    // strings first (protect them from keyword matching)
    var parts = [];
    var re = /("(?:[^"\\]|\\.)*"|\/\/[^\n]*)/g;
    var last = 0, m;
    var out = "";
    function codeSeg(seg) {
      seg = esc(seg);
      // comments already handled; numbers
      seg = seg.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="tok-num">$1</span>');
      // types
      TYPES.forEach(function (t) {
        seg = seg.replace(new RegExp("\\b" + t + "\\b(?!\\s*\\()", "g"),
          '<span class="tok-type">' + t + "</span>");
      });
      // keywords
      KEYWORDS.forEach(function (k) {
        seg = seg.replace(new RegExp("\\b" + k + "\\b", "g"),
          '<span class="tok-kw">' + k + "</span>");
      });
      // function calls
      seg = seg.replace(/\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(?=\()/g, '<span class="tok-fn">$1</span>');
      // JSX-ish tags
      seg = seg.replace(/(&lt;\/?)([a-zA-Z][a-zA-Z0-9]*)/g, '$1<span class="tok-tag">$2</span>');
      return seg;
    }
    while ((m = re.exec(src)) !== null) {
      out += codeSeg(src.slice(last, m.index));
      var tok = m[0];
      if (tok.indexOf("//") === 0) out += '<span class="tok-com">' + esc(tok) + "</span>";
      else out += '<span class="tok-str">' + esc(tok) + "</span>";
      last = m.index + tok.length;
    }
    out += codeSeg(src.slice(last));
    return out;
  }

  document.querySelectorAll("pre code.language-nova").forEach(function (el) {
    el.innerHTML = highlight(el.textContent);
  });

  /* shared with runnable.js: the same Nova highlighter for live widgets */
  window.NovaHighlight = highlight;
})();
