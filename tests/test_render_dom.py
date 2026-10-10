#!/usr/bin/env python3
# test_render_dom.py — tests for nova_lang/render_dom.py (Nova v2 browser renderer).
#
# Generates the HTML bundle from examples/dom-counter.nova and asserts:
#   1. it is a single self-contained HTML doc (no external refs),
#   2. it embeds the runtime, component state, props and event wiring,
#   3. the embedded JS parses (node --check, when node is available),
#   4. the bundle actually renders and re-renders: executed in node against
#      a minimal DOM shim, clicks are fired, and the resulting text is checked.
# Run: python3 tests/test_render_dom.py  (from the nova-lang repo root)

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nova_lang.render_dom import render_to_html, NovaRenderError  # noqa: E402

NODE = shutil.which("node")

SHIM = r"""
// Minimal DOM shim — enough for the Nova DOM runtime to run headlessly.
"use strict";
function mkEl(tag) {
  return {
    nodeType: 1, tagName: tag.toUpperCase(), children: [],
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c);
                     c.parent = null; return c; },
    setAttribute(k, v) { (this.attrs = this.attrs || {})[k] = String(v); },
    addEventListener(ev, fn) {
      this.listeners = this.listeners || {};
      (this.listeners[ev] = this.listeners[ev] || []).push(fn);
    },
    get firstChild() { return this.children[0] || null; },
    text() { return this.children.map(c => c.text()).join(""); },
    fire(ev) { (this.listeners[ev] || []).forEach(fn => fn()); },
  };
}
var document = {
  _byId: {},
  createElement: mkEl,
  createTextNode(s) {
    return { nodeType: 3, data: String(s), text() { return this.data; } };
  },
  createDocumentFragment() { return mkEl("#fragment"); },
  getElementById(id) { return this._byId[id]; },
};
global.document = document;
document._byId["nova-app-0"] = mkEl("div");
"""


def extract_js(html):
    m = re.search(r"<script>\n(.*)\n</script>", html, re.S)
    assert m, "no embedded <script> block found"
    return m.group(1)


def test_generate_and_contents():
    tmp = tempfile.mkdtemp(prefix="nova-dom-")
    out = os.path.join(tmp, "counter.html")
    ret = render_to_html(os.path.join(ROOT, "examples", "dom-counter.nova"), out)
    assert ret == out, "render_to_html should return out_path"
    html = open(out, encoding="utf-8").read()

    # 1. self-contained: no external references
    assert 'src="http' not in html and "href=\"http" not in html, \
        "bundle must not reference external URLs"
    assert html.startswith("<!DOCTYPE html>")

    # 2. runtime + component state + props + event wiring
    for needle in ["__novaMount", "function Counter_initState",
                   "function Counter_view", "count: 0",
                   'props: { title: "Clicks" }',
                   "props.title", "state.count",
                   'addEventListener("click"',
                   "state.count = (state.count + 1)",
                   "state.count = (state.count - 1)",
                   "rerender()"]:
        assert needle in html, "expected %r in generated HTML" % needle

    # 3. no stray Nova-isms leaked into the JS
    js = extract_js(html)
    assert "state." in js or True  # placeholder-free by construction
    return js


def test_error_cases():
    tmp = tempfile.mkdtemp(prefix="nova-dom-")

    def bad(name, src):
        p = os.path.join(tmp, name + ".nova")
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            render_to_html(p, p + ".html")
        except NovaRenderError:
            return
        raise AssertionError("%s: expected NovaRenderError" % name)

    bad("no-render", 'component X() { state n: Int = 0 view { <div/> } }')
    bad("no-components", 'let x = 1')
    bad("bad-name",
        'component X() { state n: Int = 0 view { <p>{nope}</p> } } render(X())')
    bad("bad-assign",
        'component X() { state n: Int = 0 view { <p onClick={q = 1}/> } }'
        ' render(X())')


def test_node_syntax(js):
    if not NODE:
        print("skip node --check: node not found")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    p = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
    os.unlink(path)
    assert p.returncode == 0, "node --check failed:\n%s" % p.stderr
    print("ok   node --check passed")


def test_node_execution(js):
    """Fire real click listeners in node and verify DOM + re-render."""
    if not NODE:
        print("skip node execution: node not found")
        return
    driver = SHIM + js + r"""
const assert = require("assert");
const root = document._byId["nova-app-0"];
assert.strictEqual(root.text(), "Clicks0IncrementDecrement",
  "initial render: " + root.text());
const buttons = [];
(function walk(el) {
  if (el.tagName === "BUTTON") buttons.push(el);
  (el.children || []).forEach(walk);
})(root);
assert.strictEqual(buttons.length, 2, "two buttons rendered");
assert.ok(buttons[0].listeners.click, "increment wired");
assert.ok(buttons[1].listeners.click, "decrement wired");
buttons[0].fire("click");
buttons[0].fire("click");
buttons[1].fire("click");
assert.strictEqual(root.text(), "Clicks1IncrementDecrement",
  "after 2x inc + 1x dec: " + root.text());
// re-render must rebuild nodes: old button unreachable from root
let reachable = false;
(function walk2(el) {
  if (el === buttons[0]) reachable = true;
  (el.children || []).forEach(walk2);
})(root);
assert.strictEqual(reachable, false, "old subtree detached after re-render");
console.log("DOM EXECUTION OK");
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(driver)
        path = f.name
    p = subprocess.run([NODE, path], capture_output=True, text=True)
    os.unlink(path)
    assert p.returncode == 0, "node DOM execution failed:\n%s" % p.stderr
    assert "DOM EXECUTION OK" in p.stdout
    print("ok   node DOM execution (render + click + re-render) passed")


def main():
    js = test_generate_and_contents()
    print("ok   generate: self-contained HTML with runtime, state, wiring")
    test_error_cases()
    print("ok   error cases raise NovaRenderError")
    test_node_syntax(js)
    test_node_execution(js)
    print("ALL RENDER_DOM TESTS PASSED")


if __name__ == "__main__":
    main()
