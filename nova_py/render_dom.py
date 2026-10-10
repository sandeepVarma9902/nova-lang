# render_dom.py — Nova v2 browser renderer (item 5 of the v2 roadmap).
#
# Compiles Nova components to a SINGLE self-contained .html file with an
# embedded JS runtime (no external deps, no CDN — works by double-clicking).
#
#   render_to_html(source_path, out_path) -> out_path
#
# Supported (honest subset of v1 — anything else raises NovaRenderError):
#   * components with `state` fields of type Int/Float/String/Bool/List[...]
#     initialized from literal expressions
#   * component params (used in the view as {title}); supplied via the
#     top-level `render(Counter(title: "Clicks"))` call
#   * a `view { ... }` block producing plain element trees (lowercase HTML
#     tags only — nested components are rejected)
#   * text nodes, interpolated expressions {count}, string/static attributes
#   * event attributes onClick={...} (and on<Event>={...} in general)
#   * inline handler expressions that mutate state, e.g.
#         onClick={count = count + 1}      (Assign to a state field)
#         onClick={items.push("Buy milk")} (method calls on state lists)
#     Handlers run, then the component subtree fully re-renders.
#   * <for item in items> over a state list / param list
#
# NOT supported (honest limits):
#   * component methods / `fn` inside a component — the v1 parser does not
#     accept them; handlers are inline expressions by design
#   * nested components (<Counter> inside a view)
#   * `this.` outside of simple state access, Option/Result match, closures,
#     imports, top-level statements other than component decls and
#     render(...) mounts (they are ignored — console-render artifacts)
#   * CSS classes beyond plain `class="..."` attributes (no <style> blocks)
#
# Re-render strategy: full subtree rebuild on every state change
# (root.innerHTML = ""; root.append(view())). Simple, correct, and fast
# enough for Nova's v1 target examples.

from __future__ import annotations

import html as _html
import json as _json
import os

from . import ast as A
from .checker import check_source
from .parser import ParseError, JsxError


class NovaRenderError(Exception):
    """Raised when a Nova program cannot be compiled to a DOM bundle."""

    def __init__(self, message, node=None):
        if node is not None and getattr(node, "line", 0):
            message = "%s (line %d, col %d)" % (message, node.line, node.col)
        super().__init__(message)


# --------------------------------------------------------------------------
# JS runtime (embedded verbatim in the generated HTML)
# --------------------------------------------------------------------------
RUNTIME_JS = """/* Nova DOM runtime v1 — embedded, zero dependencies.
 * Each component is compiled to a view function:
 *     function <Name>_view(state, props, rerender) -> DOM node
 * Mounting copies the initial state, renders once, and any event handler
 * mutates `state` then calls rerender(), which rebuilds the whole subtree.
 */
function __novaMount(rootEl, def) {
  var state = def.initState();
  var props = def.props;
  function rerender() {
    while (rootEl.firstChild) rootEl.removeChild(rootEl.firstChild);
    rootEl.appendChild(def.view(state, props, rerender));
  }
  rerender();
  return { state: state, props: props, rerender: rerender };
}
function __novaText(s) {
  return document.createTextNode(s === null || s === undefined ? "" : String(s));
}
"""

# --------------------------------------------------------------------------
# Expression -> JS
# --------------------------------------------------------------------------

_BINARY_OPS = {
    "+": "+", "-": "-", "*": "*", "/": "%", "%": "%",
    "==": "===", "!=": "!==",
    "<": "<", "<=": "<=", ">": ">", ">=": ">=",
    "and": "&&", "or": "||",
}
_UNARY_OPS = {"not": "!", "-": "-"}


class _Scope:
    """Names visible in the current JS scope."""

    def __init__(self, states, params, loopvars=()):
        self.states = set(states)
        self.params = set(params)
        self.loopvars = set(loopvars)

    def child_loop(self, var):
        return _Scope(self.states, self.params, self.loopvars | {var})


class _ExprCompiler:
    def __init__(self, comp_name, scope, init_only=False):
        self.comp_name = comp_name
        self.scope = scope
        self.init_only = init_only  # state init: no Var refs allowed

    def err(self, node, msg):
        raise NovaRenderError("in <%s>: %s" % (self.comp_name, msg), node)

    def c(self, node):
        """Compile an expression node to a JS expression string."""
        if isinstance(node, A.IntLit):
            return str(node.value)
        if isinstance(node, A.FloatLit):
            return repr(node.value)
        if isinstance(node, A.StrLit):
            return _json.dumps(node.value)
        if isinstance(node, A.BoolLit):
            return "true" if node.value else "false"
        if isinstance(node, A.NoneLit):
            return "null"
        if isinstance(node, A.ListLit):
            return "[" + ", ".join(self.c(e) for e in node.elems) + "]"
        if isinstance(node, A.Var):
            return self.var_ref(node)
        if isinstance(node, A.This):
            return "state"
        if isinstance(node, A.Field):
            return "%s.%s" % (self.c(node.obj), node.name)
        if isinstance(node, A.Index):
            return "%s[%s]" % (self.c(node.obj), self.c(node.index))
        if isinstance(node, A.Binary):
            op = _BINARY_OPS.get(node.op)
            if op is None:
                self.err(node, "operator '%s' not supported in DOM render" % node.op)
            return "(%s %s %s)" % (self.c(node.left), op, self.c(node.right))
        if isinstance(node, A.Unary):
            op = _UNARY_OPS.get(node.op)
            if op is None:
                self.err(node, "operator '%s' not supported in DOM render" % node.op)
            return "(%s%s)" % (op, self.c(node.expr))
        if isinstance(node, A.Assign):
            return self.assign(node)
        if isinstance(node, A.MethodCall):
            return self.method_call(node)
        if isinstance(node, A.Call):
            self.err(node, "function calls are not supported in DOM render")
        if isinstance(node, A.IfExpr):
            return self.if_expr(node)
        return self.err(node, "expression of type %s not supported in DOM render"
                         % type(node).__name__)

    def var_ref(self, node):
        if self.init_only:
            self.err(node, "state initializers must be literal (no variable refs)")
        name = node.name
        if name in self.scope.states:
            return "state." + name
        if name in self.scope.params:
            return "props." + name
        if name in self.scope.loopvars:
            return name
        self.err(node, "unknown name '%s' (not a state field, param, or loop var)"
                 % name)

    def assign(self, node):
        t = node.target
        if not isinstance(t, A.Var):
            self.err(node, "only simple state assignments are supported in handlers")
        if t.name not in self.scope.states:
            self.err(node, "cannot assign to '%s' — only state fields can be "
                     "assigned in handlers" % t.name)
        return "state.%s = %s" % (t.name, self.c(node.value))

    def method_call(self, node):
        obj = self.c(node.obj)
        args = ", ".join(self.c(a) for _, a in node.args)
        if node.name == "push":
            return "%s.push(%s)" % (obj, args)
        self.err(node, "method '%s' not supported in DOM render" % node.name)

    def if_expr(self, node):
        cond = self.c(node.cond)
        then_v = self.block_value(node.then_b)
        else_v = self.block_value(node.else_b)
        return "(%s ? %s : %s)" % (cond, then_v, else_v)

    def block_value(self, block):
        if block is None or not getattr(block, "stmts", []):
            return "undefined"
        stmts = block.stmts
        if len(stmts) != 1 or not isinstance(stmts[0], A.ExprStmt):
            raise NovaRenderError("only single-expression if-branches are supported "
                                  "in DOM render", stmts[0] if stmts else None)
        return self.c(stmts[0].expr)


# --------------------------------------------------------------------------
# View (JSX) -> JS
# --------------------------------------------------------------------------

class _ViewCompiler:
    def __init__(self, comp_name, states, params):
        self.comp_name = comp_name
        self.scope = _Scope(states, params)
        self._n = 0

    def err(self, node, msg):
        raise NovaRenderError("in <%s> view: %s" % (self.comp_name, msg), node)

    def tmp(self, prefix):
        self._n += 1
        return "__n%d_%s" % (self._n, prefix)

    def expr(self, node, scope=None):
        return _ExprCompiler(self.comp_name,
                             scope or self.scope).c(node)

    def compile_view_fn(self, view_nodes):
        lines = []
        lines.append("function %s_view(state, props, rerender) {" % self.comp_name)
        lines.append("  var __root = document.createDocumentFragment();")
        for ch in view_nodes:
            lines.extend(self.compile_child(ch, self.scope, "__root", "  "))
        lines.append("  return __root;")
        lines.append("}")
        return "\n".join(lines)

    def compile_child(self, node, scope, parent_var, ind):
        """Append statements that render `node` and append it to parent_var."""
        lines = []
        if isinstance(node, A.JText):
            el = self.tmp("t")
            lines.append('%svar %s = __novaText(%s);' %
                         (ind, el, _json.dumps(node.text)))
            lines.append("%s%s.appendChild(%s);" % (ind, parent_var, el))
        elif isinstance(node, A.JExpr):
            el = self.tmp("x")
            lines.append("%svar %s = __novaText(%s);" %
                         (ind, el, self.expr(node.expr, scope)))
            lines.append("%s%s.appendChild(%s);" % (ind, parent_var, el))
        elif isinstance(node, A.JFor):
            lines.extend(self.compile_for(node, scope, parent_var, ind))
        elif isinstance(node, A.JElement):
            if node.is_component:
                self.err(node, "nested components (<%s>) are not supported in "
                         "DOM render v1" % node.tag)
            el = self.tmp("e")
            lines.append('%svar %s = document.createElement(%s);' %
                         (ind, el, _json.dumps(node.tag)))
            for attr in node.attrs:
                lines.extend(self.compile_attr(attr, el, scope, ind))
            for ch in node.children:
                lines.extend(self.compile_child(ch, scope, el, ind))
            lines.append("%s%s.appendChild(%s);" % (ind, parent_var, el))
        else:
            self.err(node, "view node %s not supported in DOM render"
                     % type(node).__name__)
        return lines

    def compile_attr(self, attr, el, scope, ind):
        lines = []
        name = attr.name
        if name.startswith("on") and len(name) > 2 and name[2].isupper():
            # onClick={...} -> addEventListener("click", ...)
            if attr.value is None or isinstance(attr.value, A.BoolLit):
                self.err(attr, "event attribute '%s' needs a handler expression" % name)
            ev = name[2:].lower()
            handler_js = self.expr(attr.value, scope)
            lines.append('%s%s.addEventListener(%s, function() { %s; rerender(); });' %
                         (ind, el, _json.dumps(ev), handler_js))
        elif isinstance(attr.value, A.StrLit):
            lines.append('%s%s.setAttribute(%s, %s);' %
                         (ind, el, _json.dumps(name),
                          _json.dumps(attr.value.value)))
        elif isinstance(attr.value, A.BoolLit) and attr.value.value:
            lines.append('%s%s.setAttribute(%s, "");' % (ind, el, _json.dumps(name)))
        elif attr.value is None:
            lines.append('%s%s.setAttribute(%s, "");' % (ind, el, _json.dumps(name)))
        else:
            lines.append('%s%s.setAttribute(%s, String(%s));' %
                         (ind, el, _json.dumps(name), self.expr(attr.value, scope)))
        return lines

    def compile_for(self, node, scope, parent_var, ind):
        if not isinstance(node.iter, A.Var):
            self.err(node, "<for> iter must be a state/param list name in DOM render v1")
        iter_js = _ExprCompiler(self.comp_name, scope).var_ref(node.iter)
        item = node.var
        child_scope = scope.child_loop(item)
        loop = self.tmp("i")
        lines = []
        lines.append("%sfor (var %s of %s) {" % (ind, loop, iter_js))
        # rename the loop variable to the Nova name for child compilation
        lines.append("%s  var %s = %s;" % (ind, item, loop))
        for ch in node.children:
            lines.extend(self.compile_child(ch, child_scope, parent_var, ind + "  "))
        lines.append("%s}" % ind)
        return lines


# --------------------------------------------------------------------------
# Component -> JS
# --------------------------------------------------------------------------

def _type_name(t):
    return t.name if t is not None else "?"


def compile_component(decl):
    comp = decl.name
    states = [s.name for s in decl.states]
    params = [p.name for p in decl.params]

    # --- state init ---
    init_parts = []
    for s in decl.states:
        ec = _ExprCompiler(comp, _Scope(states, params), init_only=True)
        try:
            init_js = ec.c(s.init) if s.init is not None else "null"
        except NovaRenderError as e:
            raise NovaRenderError("state '%s': %s" % (s.name, e), s.init)
        init_parts.append("    %s: %s  /* %s */" % (s.name, init_js,
                                                   _type_name(s.type)))
    init_fn = ("function %s_initState() {\n  return {\n%s\n  };\n}" %
               (comp, ",\n".join(init_parts)))

    # --- view ---
    vc = _ViewCompiler(comp, states, params)
    view_fn = vc.compile_view_fn(decl.view)

    return init_fn + "\n\n" + view_fn


# --------------------------------------------------------------------------
# Program -> mounts
# --------------------------------------------------------------------------

def _find_mounts(program, components):
    """Top-level `render(C(...))` (bare or let-bound) -> [(comp, props_js, var)]."""
    mounts = []
    for stmt in program.stmts:
        call = None
        if isinstance(stmt, A.ExprStmt):
            call = stmt.expr
        elif isinstance(stmt, A.LetDecl) and isinstance(stmt.expr, A.Call):
            call = stmt.expr
        elif isinstance(stmt, (A.ComponentDecl, A.Import)):
            continue
        else:
            # console-render artifacts (app.fire(...), prints, match, fns...)
            continue
        if not (isinstance(call, A.Call) and isinstance(call.func, A.Var)
                and call.func.name == "render"):
            continue
        if len(call.args) != 1 or call.args[0][0] is not None:
            raise NovaRenderError("render(...) takes exactly one positional argument",
                                  call)
        inst = call.args[0][1]
        if not (isinstance(inst, A.Call) and isinstance(inst.func, A.Var)
                and inst.func.name in components):
            raise NovaRenderError("render(...) argument must be a component "
                                  "constructor, e.g. render(Counter(title: \"x\"))",
                                  inst)
        comp = inst.func.name
        decl = components[comp]
        props = []
        for name, expr in inst.args:
            if name is None:
                raise NovaRenderError("component props must be named, e.g. "
                                      "Counter(title: \"x\")", inst)
            ec = _ExprCompiler(comp, _Scope([], [p.name for p in decl.params]),
                               init_only=True)
            props.append("%s: %s" % (name, ec.c(expr)))
        mounts.append((comp, props))
    return mounts


def compile_program(program):
    components = {}
    for stmt in program.stmts:
        if isinstance(stmt, A.ComponentDecl):
            if stmt.name in components:
                raise NovaRenderError("duplicate component <%s>" % stmt.name, stmt)
            components[stmt.name] = stmt
    if not components:
        raise NovaRenderError("no components found in source")
    mounts = _find_mounts(program, components)
    if not mounts:
        raise NovaRenderError("no render(...) call found — add e.g. "
                              "render(Counter(title: \"x\")) at the end of the file")
    js_parts = [compile_component(components[c]) for c in
                dict.fromkeys(comp for comp, _ in mounts)]
    mount_lines = []
    for i, (comp, props) in enumerate(mounts):
        mount_lines.append(
            "__novaMount(document.getElementById('nova-app-%d'), {\n"
            "  initState: %s_initState,\n"
            "  props: { %s },\n"
            "  view: %s_view\n"
            "});" % (i, comp, ", ".join(props) if props else "", comp))
    return js_parts, mount_lines, len(mounts)


# --------------------------------------------------------------------------
# HTML assembly + public API
# --------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nova DOM &mdash; {title}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem}}</style>
</head>
<body>
<!-- Generated by Nova render_dom v1 from {source_name}.
     Self-contained: no network, no dependencies. -->
{containers}
<script>
"use strict";
{runtime}
// ---- compiled components ----
{components}
// ---- mounts ----
{mounts}
</script>
</body>
</html>
"""


def render_to_html(source_path, out_path):
    """Compile a Nova source file to a self-contained HTML DOM bundle.

    Returns out_path. Raises NovaRenderError on unsupported constructs.
    """
    source_path = os.path.abspath(source_path)
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()

    _, diagnostics = check_source(source, source_path,
                                  os.path.dirname(source_path))
    if diagnostics:
        d = diagnostics[0]
        raise NovaRenderError("type error [%s]: %s" % (d.code, d.message))

    from .parser import parse_source
    try:
        program = parse_source(source, source_path)
    except (ParseError, JsxError) as e:
        raise NovaRenderError("syntax error: %s" % e)

    js_parts, mount_lines, n_mounts = compile_program(program)

    comp_names = [s.name for s in program.stmts
                  if isinstance(s, A.ComponentDecl)]
    title = _html.escape(comp_names[0] if comp_names else "app")
    source_name = _html.escape(os.path.basename(source_path))
    containers = "\n".join(
        '<div id="nova-app-%d"></div>' % i for i in range(n_mounts))

    doc = HTML_TEMPLATE.format(
        title=title,
        source_name=source_name,
        containers=containers,
        runtime=RUNTIME_JS,
        components="\n\n".join(js_parts),
        mounts="\n".join(mount_lines),
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path
