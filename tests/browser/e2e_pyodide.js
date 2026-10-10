// Real-Pyodide no-thread e2e for Nova.
// Loads REAL Pyodide in Node, writes the 25 vendored nova_lang/*.py files via
// pyodide.FS, extracts the EXACT harness from website/js/nova-live.js, and runs
// every runnable snippet from website/docs/*.html + website/js/examples.json.
// --nothreads patches threading.Thread.start to raise RuntimeError, simulating
// the GitHub Pages browser environment (no COOP/COEP -> no OS threads).
// Zero failures required before shipping.
"use strict";

const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname + "/../../website");
const NO_THREADS = process.argv.includes("--nothreads");

async function main() {
  const { loadPyodide } = require("pyodide");
  const pyodide = await loadPyodide();

  // Vendor the 25 files into /nova_py/nova_lang/ exactly like nova-live.js does.
  const srcDir = path.join(REPO, "nova_py");
  const files = JSON.parse(fs.readFileSync(path.join(srcDir, "files.json"), "utf8")).files;
  if (files.length !== 25) throw new Error("expected 25 vendored files, got " + files.length);
  pyodide.FS.mkdirTree("/nova_py/nova_lang");
  for (const f of files) {
    const data = fs.readFileSync(path.join(srcDir, f));
    pyodide.FS.writeFile("/nova_py/nova_lang/" + f, data);
  }
  pyodide.FS.writeFile("/nova_py/nova_lang/__init__.py", "");

  // Extract the EXACT harness from nova-live.js (the HARNESS array literal).
  const liveSrc = fs.readFileSync(path.join(REPO, "js/nova-live.js"), "utf8");
  const m = liveSrc.match(/var HARNESS = \[([\s\S]*?)\]\.join\("\\n"\);/);
  if (!m) throw new Error("could not extract HARNESS from nova-live.js");
  const harness = eval("[" + m[1] + '].join("\\n")');

  let setup = "import sys\nsys.path.insert(0, '/nova_py')\n";
  if (NO_THREADS) {
    setup += "import threading\n" +
      "orig_start = threading.Thread.start\n" +
      "def _boom(self):\n" +
      "    raise RuntimeError(\"can't start new thread\")\n" +
      "threading.Thread.start = _boom\n";
  }
  pyodide.runPython(setup + harness);

  // Collect runnable snippets: docs HTML Run-widgets + examples.json.
  const snippets = [];
  const docsDir = path.join(REPO, "docs");
  for (const f of fs.readdirSync(docsDir)) {
    if (!f.endsWith(".html")) continue;
    const html = fs.readFileSync(path.join(docsDir, f), "utf8");
    // Run widgets are rendered from ```nova fences; recover them from the
    // highlighted source stored in data-source attributes if present, else
    // from <pre class="nova-src"> blocks. Fall back: skip page.
    // Runnable blocks: <pre><code class="language-nova"> WITHOUT data-norun.
    // (tempered pattern: the attribute string must not contain data-norun)
    const re = /<pre><code class="language-nova"((?:(?!data-norun)[^>])*)>([\s\S]*?)<\/code><\/pre>/g;
    let mm;
    while ((mm = re.exec(html)) !== null) {
      const src = mm[2]
        .replace(/<[^>]+>/g, "")
        .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
        .replace(/&#x27;/g, "'").replace(/&#39;/g, "'").replace(/&amp;/g, "&");
      if (src.trim()) snippets.push({ page: f, source: src });
    }
  }
  const examples = JSON.parse(fs.readFileSync(path.join(REPO, "js/examples.json"), "utf8"));
  for (const e of examples) snippets.push({ page: "examples.json:" + e.slug, source: e.source });

  console.log("snippets collected:", snippets.length, NO_THREADS ? "(nothreads)" : "");
  let fails = 0;
  for (const s of snippets) {
    pyodide.globals.set("__nova_src", s.source);
    let res;
    try {
      res = JSON.parse(pyodide.runPython("nova_run(__nova_src)"));
    } catch (e) {
      console.log("CRASH", s.page, String(e).slice(0, 120));
      fails++;
      continue;
    }
    if (!res.ok) {
      console.log("FAIL", s.page, "->", String(res.error).slice(0, 160).replace(/\n/g, " | "));
      fails++;
    }
  }
  console.log(fails + "/" + snippets.length + " snippets failed");
  process.exit(fails ? 1 : 0);
}

main().catch((e) => { console.error("HARNESS FAILURE:", e.message); process.exit(2); });
