# checker.py — Nova static type checker.
#
# Runs before interpretation. Every diagnostic carries a stable error code
# and a suggested fix, and `nova check` emits them as JSON for AI consumers.

import os
from . import ast as A
from .lexer import Lexer
from .parser import Parser, ParseError, JsxError
from .types import (Type, PrimType, INT, FLOAT, STRING, BOOL, UNIT, ANY,
                    ListType, OptionType, ResultType, ClassType,
                    InterfaceType, ComponentType, InstanceType, ModuleType,
                    TaskType, StreamType, SubscriptionType, MessageType,
                    is_subtype, ClassInfo)

INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1

PURE = frozenset()
CONSOLE = frozenset({"console"})
IO_EFF = frozenset({"console", "io"})
NET_EFF = frozenset({"console", "net"})
TOP_EFF = frozenset({"console", "io", "net"})
# async fns may await and print; blocking IO inside them is E_ASYNC_BLOCKING
# unless wrapped in spawn { }. They compose with io/net for offloaded IO.
ASYNC_EFF = frozenset({"async", "console"})
ASYNC_IO_EFF = frozenset({"async", "console", "io"})
ASYNC_NET_EFF = frozenset({"async", "console", "net"})

EFFECT_OF = {"pure": PURE, "effect": CONSOLE, "io": IO_EFF, "net": NET_EFF,
             "async": ASYNC_EFF, "async io": ASYNC_IO_EFF,
             "async net": ASYNC_NET_EFF}

# builtins that block the calling thread: forbidden directly inside async fns
BLOCKING_BUILTINS = {"readFile", "writeFile", "fetch"}

BANNED_IDENTS = {"null", "nil", "undefined"}

CAP_TYPES = {"IO", "Net"}


class Diagnostic:
    def __init__(self, code, message, line, col, hint=""):
        self.code = code
        self.message = message
        self.line = line
        self.col = col
        self.hint = hint

    def to_dict(self):
        d = {"code": self.code, "message": self.message,
             "line": self.line, "col": self.col}
        if self.hint:
            d["hint"] = self.hint
        return d


class FuncSig:
    def __init__(self, name, params, ret, effect):
        self.name = name
        self.params = params      # [(name, Type)]
        self.ret = ret            # Type
        self.effect = effect      # frozenset

    def __repr__(self):
        return "fn %s" % self.name


class Symbol:
    def __init__(self, name, type, kind, mutable=True):
        self.name = name
        self.type = type
        self.kind = kind          # let|var|param|state|field|fn|class|...
        self.mutable = mutable


class Scope:
    def __init__(self, parent=None):
        self.parent = parent
        self.symbols = {}

    def define(self, sym):
        self.symbols[sym.name] = sym

    def lookup(self, name):
        s = self
        while s is not None:
            if name in s.symbols:
                return s.symbols[name]
            s = s.parent
        return None


class CheckError(Exception):
    pass


class Checker:
    def __init__(self, basedir, filename, is_entry=True, seen=None):
        self.basedir = basedir
        self.filename = filename
        self.is_entry = is_entry
        self.errors = []
        self.classes = {}       # name -> ClassInfo
        self.class_nodes = {}   # name -> ClassDecl
        self.interfaces = {}    # name -> InterfaceDecl
        self.components = {}   # name -> ComponentDecl
        self.modules = {}       # name -> exports dict
        self.func_sigs = {}     # name -> FuncSig (top-level fns)
        self.global_scope = Scope()
        self.effect = TOP_EFF if is_entry else CONSOLE
        self.in_ai_body = False
        self.in_ensures = False
        self.in_init = False
        self.in_spawn = False
        self.in_spawn_async = False
        self.in_async_fn = False
        self.contexts = {}        # name -> Type (declared contexts)
        self.current_class = None
        self.current_component = None
        self.seen = seen if seen is not None else set()
        self._scope_stack = []
        self._fn_ret = UNIT
        if is_entry:
            # The trust root: the entry module receives capabilities from
            # the runtime. Imported modules must receive them as parameters.
            self.global_scope.define(
                Symbol("io", _Cap("IO"), "cap", mutable=False))
            self.global_scope.define(
                Symbol("net", _Cap("Net"), "cap", mutable=False))

    # -- diagnostics --------------------------------------------------
    def error(self, code, message, node, hint=""):
        line = getattr(node, "line", 0)
        col = getattr(node, "col", 0)
        self.errors.append(Diagnostic(code, message, line, col, hint))

    # -- types --------------------------------------------------------
    def resolve_type(self, tnode):
        name = tnode.name
        args = tnode.args
        if name == "Int":
            self._no_args(tnode, name)
            return INT
        if name == "Float":
            self._no_args(tnode, name)
            return FLOAT
        if name == "String":
            self._no_args(tnode, name)
            return STRING
        if name == "Bool":
            self._no_args(tnode, name)
            return BOOL
        if name == "Unit":
            self._no_args(tnode, name)
            return UNIT
        if name == "List":
            self._n_args(tnode, name, 1)
            return ListType(self.resolve_type(args[0]))
        if name == "Option":
            self._n_args(tnode, name, 1)
            return OptionType(self.resolve_type(args[0]))
        if name == "Result":
            self._n_args(tnode, name, 2)
            return ResultType(self.resolve_type(args[0]),
                              self.resolve_type(args[1]))
        if name == "Task":
            self._n_args(tnode, name, 1)
            return TaskType(self.resolve_type(args[0]))
        if name == "Stream":
            self._n_args(tnode, name, 1)
            return StreamType(self.resolve_type(args[0]))
        if name == "Subscription":
            self._n_args(tnode, name, 1)
            return SubscriptionType(self.resolve_type(args[0]))
        if name == "Message":
            self._n_args(tnode, name, 1)
            return MessageType(self.resolve_type(args[0]))
        if name in CAP_TYPES:
            self._no_args(tnode, name)
            return _Cap(name)
        if name in self.classes:
            self._no_args(tnode, name)
            return ClassType(name)
        if name in self.interfaces:
            self._no_args(tnode, name)
            return InterfaceType(name)
        self.error("E_UNDEFINED_TYPE", "unknown type '%s'" % name, tnode,
                   hint="did you declare class/interface %s?" % name)
        return ANY

    def _no_args(self, tnode, name):
        if tnode.args:
            self.error("E_TYPE_ARGS", "%s takes no type arguments" % name,
                       tnode)

    def _n_args(self, tnode, name, n):
        if len(tnode.args) != n:
            self.error("E_TYPE_ARGS",
                       "%s needs %d type argument(s)" % (name, n), tnode)

    def check_compatible(self, expected, actual, node, what="value"):
        if expected == ANY or actual == ANY:
            return True
        # the empty list literal [] has type List[Any] and fits any List[T]
        if (isinstance(expected, ListType) and isinstance(actual, ListType)
                and actual.elem == ANY):
            return True
        if isinstance(actual, OptionType) and not isinstance(expected, OptionType):
            self.error("E_UNHANDLED_OPTION",
                       "%s has type %s but %s is needed"
                       % (what, actual, expected), node,
                       hint="handle it with match, or use ? inside a "
                            "function returning Option")
            return False
        if is_subtype(actual, expected, self.classes):
            return True
        self.error("E_TYPE_MISMATCH",
                   "%s has type %s but %s is needed" % (what, actual, expected),
                   node, hint="Nova has no implicit conversions")
        return False

    # -- program ------------------------------------------------------
    def check_program(self, program):
        # Pass 1: register declarations.
        for stmt in program.stmts:
            self.register(stmt)
        # Pass 2: check bodies.
        for stmt in program.stmts:
            self.check_top(stmt)
        return self.exports()

    def exports(self):
        out = {}
        for name, sym in self.global_scope.symbols.items():
            if name.startswith("_") or name in ("io", "net"):
                continue
            out[name] = sym
        return out

    def register(self, node):
        if isinstance(node, A.Import):
            self.do_import(node)
        elif isinstance(node, A.FnDecl):
            self.register_fn(node)
        elif isinstance(node, A.AiDecl):
            self.register_ai(node)
        elif isinstance(node, A.ClassDecl):
            self.register_class(node)
        elif isinstance(node, A.InterfaceDecl):
            if node.name in self.interfaces:
                self.error("E_DUPLICATE",
                           "interface %s already defined" % node.name, node)
            else:
                self.interfaces[node.name] = node
                self.global_scope.define(Symbol(node.name,
                                                InterfaceType(node.name),
                                                "interface", mutable=False))
        elif isinstance(node, A.ComponentDecl):
            if node.name in self.components:
                self.error("E_DUPLICATE",
                           "component %s already defined" % node.name, node)
            else:
                self.components[node.name] = node
                self.global_scope.define(Symbol(node.name,
                                                ComponentType(node.name),
                                                "component", mutable=False))
        # let/var checked in pass 2 (need full type table first)

    def check_top(self, node):
        if isinstance(node, (A.Import,)):
            return
        if isinstance(node, A.FnDecl):
            self.check_fn(node, None)
        elif isinstance(node, A.AiDecl):
            self.check_ai(node)
        elif isinstance(node, A.ClassDecl):
            self.check_class(node)
        elif isinstance(node, A.InterfaceDecl):
            pass
        elif isinstance(node, A.ComponentDecl):
            self.check_component(node)
        elif isinstance(node, (A.LetDecl, A.VarDecl)):
            self.check_let_var(node, self.global_scope)
        elif isinstance(node, A.StreamDecl):
            self.check_stream_decl(node, self.global_scope)
        else:
            # top-level statements run in the entry effect context
            self.check_stmt(node, self.global_scope)

    # -- imports ------------------------------------------------------
    def do_import(self, node):
        if node.name in self.modules:
            return
        from .modpath import resolve
        path = resolve(node.name, self.basedir)
        if path is None:
            self.error("E_IMPORT", "module '%s' not found" % node.name, node,
                       hint="modules live next to the importing file as "
                            "%s.nova, or in the stdlib" % node.name)
            return
        real = os.path.realpath(path)
        if real in self.seen:
            self.error("E_IMPORT", "import cycle involving '%s'" % node.name,
                       node)
            return
        self.seen.add(real)
        try:
            with open(path) as f:
                source = f.read()
            toks = Lexer(source, path).tokenize()
            prog = Parser(toks, source, path).parse_program()
        except Exception as e:
            self.error("E_IMPORT",
                       "could not load module '%s': %s" % (node.name, e),
                       node)
            return
        sub = Checker(os.path.dirname(path), path, is_entry=False,
                      seen=self.seen)
        # share class/interface tables so types unify across modules
        sub.classes = self.classes
        sub.class_nodes = self.class_nodes
        sub.interfaces = self.interfaces
        sub.components = self.components
        sub.func_sigs = self.func_sigs
        sub.check_program(prog)
        self.errors.extend(sub.errors)
        self.modules[node.name] = sub.exports()
        self.global_scope.define(Symbol(node.name, ModuleType(node.name),
                                        "module", mutable=False))

    # -- functions ----------------------------------------------------
    def register_fn(self, node):
        if node.name in self.global_scope.symbols:
            self.error("E_DUPLICATE", "'%s' already defined" % node.name,
                       node)
            return
        params = [(p.name, self.resolve_type(p.type)) for p in node.params]
        ret = self.resolve_type(node.ret) if node.ret else UNIT
        sig = FuncSig(node.name, params, ret, EFFECT_OF[node.effect])
        self.func_sigs[node.name] = sig
        self.global_scope.define(Symbol(node.name, sig, "fn", mutable=False))

    def register_ai(self, node):
        if node.name in self.global_scope.symbols:
            self.error("E_DUPLICATE", "'%s' already defined" % node.name,
                       node)
            return
        if not node.ret:
            self.error("E_AI_SCHEMA",
                       "ai fn %s needs a return type (it is the output schema)"
                       % node.name, node)
            return
        params = [(p.name, self.resolve_type(p.type)) for p in node.params]
        ret = self.resolve_type(node.ret)
        self.check_ai_schema(ret, node)
        for key in node.config:
            if key not in ("model", "temperature", "max_tokens", "stream"):
                self.error("E_AI_CONFIG", "unknown ai config '%s'" % key,
                           node, hint="known: model, temperature, "
                                      "max_tokens, stream")
        sig = FuncSig(node.name, params, ret, NET_EFF)  # ai calls use network
        self.func_sigs[node.name] = sig
        self.global_scope.define(Symbol(node.name, sig, "aifn", mutable=False))

    def check_ai_schema(self, t, node):
        # Output schema must be mockable/serializable: primitives, data
        # classes, lists/options of those.
        if isinstance(t, (type(INT),)):
            return
        if isinstance(t, ListType):
            self.check_ai_schema(t.elem, node)
            return
        if isinstance(t, OptionType):
            self.check_ai_schema(t.elem, node)
            return
        if isinstance(t, ClassType):
            info = self.classes.get(t.name)
            if info is None or not info.is_data:
                self.error("E_AI_SCHEMA",
                           "ai fn output schema must be a data class or a "
                           "primitive, got %s" % t, node,
                           hint="declare e.g. `data class Summary { ... }`")
            return
        self.error("E_AI_SCHEMA",
                   "ai fn output schema must be a data class or a primitive, "
                   "got %s" % t, node)

    def check_fn(self, node, owner):
        params = [(p.name, self.resolve_type(p.type)) for p in node.params]
        ret = self.resolve_type(node.ret) if node.ret else UNIT
        effect = EFFECT_OF[node.effect]
        if node.effect in ("io", "net", "async io", "async net"):
            cap = "IO" if node.effect in ("io", "async io") else "Net"
            if not any(isinstance(pt, _Cap) and pt.name == cap
                       for _, pt in params):
                self.error("E_CAPABILITY",
                           "effect %s function '%s' must receive its %s "
                           "capability as a parameter" % (node.effect,
                                                          node.name, cap),
                           node,
                           hint="add e.g. `cap: %s` to the parameters — "
                                "capabilities are never ambient" % cap)
        scope = Scope(self.global_scope)
        for pname, ptype in params:
            scope.define(Symbol(pname, ptype, "param", mutable=False))
        saved = self.effect
        saved_ret = self._fn_ret
        saved_async = self.in_async_fn
        self.effect = effect
        self._fn_ret = ret
        self.in_async_fn = node.effect.startswith("async")
        for req in node.requires:
            rt = self.check_expr(req, scope)
            self.check_compatible(BOOL, rt, req, "requires clause")
        self.in_ensures = False
        body_ret = self.check_block(node.body, scope)
        self.in_ensures = True
        for ens in node.ensures:
            # `result` has the declared return type inside ensures
            scope.define(Symbol("result", ret, "result", mutable=False))
            et = self.check_expr(ens, scope)
            self.check_compatible(BOOL, et, ens, "ensures clause")
            del scope.symbols["result"]
        self.in_ensures = False
        self.effect = saved
        self._fn_ret = saved_ret
        self.in_async_fn = saved_async
        # every path returns for non-Unit functions (best effort)
        if ret != UNIT and not self.block_returns(node.body):
            self.error("E_RETURN",
                       "function '%s' may exit without returning %s"
                       % (node.name, ret), node,
                       hint="end every path with `return <value>`")

    def check_ai(self, node):
        params = [(p.name, self.resolve_type(p.type)) for p in node.params]
        ret = self.resolve_type(node.ret)
        scope = Scope(self.global_scope)
        for pname, ptype in params:
            scope.define(Symbol(pname, ptype, "param", mutable=False))
        saved = self.effect
        saved_ret = self._fn_ret
        self.effect = NET_EFF
        self._fn_ret = ret
        self.in_ai_body = True
        self.check_block(node.body, scope)
        self.in_ai_body = False
        self.effect = saved
        self._fn_ret = saved_ret

    def block_returns(self, block):
        for s in block.stmts:
            if isinstance(s, A.Return):
                return True
            if isinstance(s, A.IfStmt) and s.else_b is not None:
                if self.block_returns(s.then_b) and self.block_returns(s.else_b):
                    return True
        return False

    # -- classes ------------------------------------------------------
    def register_class(self, node):
        if node.name in self.classes:
            self.error("E_DUPLICATE", "class %s already defined" % node.name,
                       node)
            return
        if node.parent and node.parent not in self.classes:
            self.error("E_UNDEFINED", "parent class '%s' not found"
                       % node.parent, node)
        for iface in node.interfaces:
            if iface not in self.interfaces:
                self.error("E_UNDEFINED", "interface '%s' not found" % iface,
                           node)
        self.classes[node.name] = ClassInfo(node.name, node.parent,
                                            node.interfaces, node.is_data)
        self.class_nodes[node.name] = node
        self.global_scope.define(Symbol(node.name, ClassType(node.name),
                                        "class", mutable=False))

    def class_fields(self, name):
        """All fields including inherited: {name: (type, mutable)}."""
        out = {}
        info = self.classes.get(name)
        chain = []
        while info:
            chain.append(info)
            info = self.classes.get(info.parent) if info.parent else None
        for info in reversed(chain):
            node = self.class_nodes[info.name]
            for m in node.members:
                if isinstance(m, A.FieldDecl):
                    out[m.name] = (self.resolve_type(m.type), m.mutable)
        return out

    def class_methods(self, name):
        """All methods including inherited: {name: MethodDecl}."""
        out = {}
        info = self.classes.get(name)
        chain = []
        while info:
            chain.append(info)
            info = self.classes.get(info.parent) if info.parent else None
        for info in reversed(chain):
            node = self.class_nodes[info.name]
            for m in node.members:
                if isinstance(m, A.MethodDecl):
                    out[m.name] = m
        return out

    def check_class(self, node):
        if node.is_data:
            # data class: fields from header; structural == / repr built in
            return
        saved_class = self.current_class
        self.current_class = node.name
        # interface conformance
        for iface in node.interfaces:
            idecl = self.interfaces[iface]
            methods = self.class_methods(node.name)
            for m in idecl.methods:
                if m.name not in methods:
                    self.error("E_INTERFACE",
                               "class %s does not implement %s.%s"
                               % (node.name, iface, m.name), node)
                else:
                    impl = methods[m.name]
                    self.check_sig_match(iface, m, impl)
                    if not impl.is_override:
                        self.error("E_OVERRIDE",
                                   "method %s implements %s.%s — mark it "
                                   "`override`" % (m.name, iface, m.name),
                                   impl)
        for m in node.members:
            if isinstance(m, A.InitDecl):
                self.check_init(node, m)
            elif isinstance(m, A.MethodDecl):
                self.check_method(node, m)
                # override checking
                parent = self.classes[node.name].parent
                overridden = False
                if parent and m.name in self.class_methods(parent):
                    overridden = True
                ifaces = []
                for iface in node.interfaces:
                    if any(mm.name == m.name
                           for mm in self.interfaces[iface].methods):
                        ifaces.append(iface)
                if (overridden or ifaces) and not m.is_override:
                    self.error("E_OVERRIDE",
                               "method %s overrides an inherited method — "
                               "mark it `override`" % m.name, m)
                if m.is_override and not (overridden or ifaces):
                    self.error("E_OVERRIDE",
                               "`override` on %s but nothing is overridden"
                               % m.name, m,
                               hint="remove `override`")
        self.current_class = saved_class

    def check_sig_match(self, iface, sig, impl):
        sp = [(p.name, self.resolve_type(p.type)) for p in sig.params]
        ip = [(p.name, self.resolve_type(p.type)) for p in impl.params]
        sr = self.resolve_type(sig.ret) if sig.ret else UNIT
        ir = self.resolve_type(impl.ret) if impl.ret else UNIT
        if len(sp) != len(ip) or any(a != b for (_, a), (_, b) in zip(sp, ip)) \
                or sr != ir:
            self.error("E_INTERFACE",
                       "method %s signature does not match %s.%s"
                       % (impl.name, iface, sig.name), impl)

    def check_init(self, cls, node):
        scope = Scope(self.global_scope)
        scope.define(Symbol("this", ClassType(cls.name), "this",
                            mutable=False))
        for p in node.params:
            scope.define(Symbol(p.name, self.resolve_type(p.type), "param",
                                mutable=False))
        saved = self.effect
        self.effect = CONSOLE  # init may print, but takes no caps implicitly
        old_init = self.in_init
        self.in_init = True
        self.check_block(node.body, scope)
        self.in_init = old_init
        self.effect = saved
        # super() requirement
        parent = self.classes[cls.name].parent
        if parent:
            pnode = self.class_nodes[parent]
            pinit = next((m for m in pnode.members
                          if isinstance(m, A.InitDecl)), None)
            if pinit is not None:
                if not any(isinstance(s, A.SuperCall) for s in node.body.stmts):
                    self.error("E_SUPER",
                               "init must call super(...) — parent %s has an "
                               "init" % parent, node)

    def check_method(self, cls, node):
        params = [(p.name, self.resolve_type(p.type)) for p in node.params]
        ret = self.resolve_type(node.ret) if node.ret else UNIT
        effect = EFFECT_OF[node.effect]
        scope = Scope(self.global_scope)
        scope.define(Symbol("this", ClassType(cls.name), "this",
                            mutable=False))
        for pname, ptype in params:
            scope.define(Symbol(pname, ptype, "param", mutable=False))
        saved = self.effect
        saved_ret = self._fn_ret
        saved_async = self.in_async_fn
        self.effect = effect
        self._fn_ret = ret
        self.in_async_fn = node.effect.startswith("async")
        for req in node.requires:
            rt = self.check_expr(req, scope)
            self.check_compatible(BOOL, rt, req, "requires clause")
        self.check_block(node.body, scope)
        self.in_ensures = True
        scope.define(Symbol("result", ret, "result", mutable=False))
        for ens in node.ensures:
            et = self.check_expr(ens, scope)
            self.check_compatible(BOOL, et, ens, "ensures clause")
        self.in_ensures = False
        self.effect = saved
        self._fn_ret = saved_ret
        self.in_async_fn = saved_async
        if ret != UNIT and not self.block_returns(node.body):
            self.error("E_RETURN",
                       "method '%s' may exit without returning %s"
                       % (node.name, ret), node)

    # -- components ---------------------------------------------------
    def check_component(self, node):
        saved = self.current_component
        self.current_component = node.name
        scope = Scope(self.global_scope)
        for p in node.params:
            scope.define(Symbol(p.name, self.resolve_type(p.type), "prop",
                                mutable=False))
        for s in node.states:
            st = self.resolve_type(s.type)
            it = self.check_expr(s.init, scope)
            self.check_compatible(st, it, s.init, "state initializer")
            scope.define(Symbol(s.name, st, "state", mutable=True))
        saved_eff = self.effect
        self.effect = PURE
        self._scope_stack.append(scope)
        for child in node.view:
            self.check_jsx(child, scope)
        self._scope_stack.pop()
        self.effect = saved_eff
        self.current_component = saved

    def check_jsx(self, node, scope):
        if isinstance(node, A.JText):
            return
        if isinstance(node, A.JExpr):
            self.check_expr(node.expr, scope)
            return
        if isinstance(node, A.JFor):
            it = self.check_expr(node.iter, scope)
            if not isinstance(it, ListType):
                self.error("E_TYPE_MISMATCH",
                           "<for> needs a List, got %s" % it, node)
                elem = ANY
            else:
                elem = it.elem
            sub = Scope(scope)
            sub.define(Symbol(node.var, elem, "let", mutable=False))
            for c in node.children:
                self.check_jsx(c, sub)
            return
        if isinstance(node, A.JElement):
            if node.is_component:
                comp = self.components.get(node.tag)
                if comp is None:
                    self.error("E_UNDEFINED",
                               "unknown component <%s>" % node.tag, node)
                    return
                self.check_named_args(
                    [(p.name, self.resolve_type(p.type))
                     for p in comp.params],
                    node.attrs, node, what="component %s" % node.tag)
            else:
                for attr in node.attrs:
                    if attr.value is None:
                        continue
                    if _is_event(attr.name):
                        saved = self.effect
                        self.effect = TOP_EFF
                        self.check_expr(attr.value, scope)
                        self.effect = saved
                    elif not isinstance(attr.value, A.StrLit):
                        self.check_expr(attr.value, scope)
            for c in node.children:
                self.check_jsx(c, scope)

    def check_named_args(self, params, attrs, node, what):
        seen = set()
        for attr in attrs:
            name = attr.name
            if _is_event(name):
                # event handler: checked in effect context
                saved = self.effect
                self.effect = TOP_EFF
                self.check_expr(attr.value, self._handler_scope())
                self.effect = saved
                continue
            match = next((pt for pn, pt in params if pn == name), None)
            if match is None:
                self.error("E_ARGC", "%s has no prop '%s'" % (what, name),
                           attr, hint="props: %s"
                           % ", ".join(pn for pn, _ in params))
                continue
            if name in seen:
                self.error("E_ARGC", "duplicate prop '%s'" % name, attr)
            seen.add(name)
            if attr.value is None:
                self.error("E_ARGC", "prop '%s' needs a value" % name, attr)
                continue
            vt = (STRING if isinstance(attr.value, A.StrLit)
                  else self.check_expr(attr.value, self._current_scope()))
            self.check_compatible(match, vt, attr.value,
                                  "prop '%s'" % name)
        missing = [pn for pn, _ in params if pn not in seen]
        if missing:
            self.error("E_ARGC", "%s missing props: %s"
                       % (what, ", ".join(missing)), node)

    def _handler_scope(self):
        return self._current_scope()

    def _current_scope(self):
        # used for prop expressions: the component scope is on the stack
        return self._scope_stack[-1]

    # -- statements ---------------------------------------------------
    def check_block(self, block, scope):
        sub = Scope(scope)
        self._scope_stack.append(sub)
        for s in block.stmts:
            self.check_stmt(s, sub)
        self._scope_stack.pop()
        return None

    def check_stmt(self, node, scope):
        if isinstance(node, (A.LetDecl, A.VarDecl)):
            self.check_let_var(node, scope)
        elif isinstance(node, A.StreamDecl):
            self.check_stream_decl(node, scope)
        elif isinstance(node, A.ContextDecl):
            self.check_context_decl(node, scope)
        elif isinstance(node, A.FnDecl):
            self.error("E_NESTED_FN", "functions cannot be nested", node,
                       hint="declare '%s' at top level" % node.name)
        elif isinstance(node, A.ExprStmt):
            self.check_expr(node.expr, scope)
        elif isinstance(node, A.Return):
            if self.in_spawn:
                self.error("E_RETURN",
                           "return cannot be used directly in a spawn block — "
                           "the block's last expression is the task's value",
                           node)
                return
            # return-type checking happens via current function record
            if node.expr is not None:
                self.check_expr(node.expr, scope)
        elif isinstance(node, A.IfStmt):
            ct = self.check_expr(node.cond, scope)
            self.check_compatible(BOOL, ct, node.cond, "if condition")
            self.check_block(node.then_b, scope)
            if node.else_b is not None:
                if isinstance(node.else_b, A.Block):
                    self.check_block(node.else_b, scope)
                else:
                    self.check_stmt(node.else_b, scope)
        elif isinstance(node, A.WhileStmt):
            ct = self.check_expr(node.cond, scope)
            self.check_compatible(BOOL, ct, node.cond, "while condition")
            self.check_block(node.body, scope)
        elif isinstance(node, A.ForStmt):
            it = self.check_expr(node.iter, scope)
            if not isinstance(it, ListType):
                self.error("E_TYPE_MISMATCH",
                           "for needs a List, got %s" % it, node.iter)
                elem = ANY
            else:
                elem = it.elem
            sub = Scope(scope)
            sub.define(Symbol(node.var, elem, "let", mutable=False))
            self.check_block(node.body, sub)
        elif isinstance(node, A.SuperCall):
            self.check_super_call(node, scope)
        else:
            self.error("E_INTERNAL", "unhandled statement %r" % node, node)

    def check_let_var(self, node, scope):
        if node.name in ("io", "net") and scope is self.global_scope:
            self.error("E_CAPABILITY",
                       "cannot shadow the '%s' capability" % node.name, node)
            return
        if node.name in scope.symbols:
            self.error("E_DUPLICATE", "'%s' already defined in this scope"
                       % node.name, node)
            return
        it = self.check_expr(node.expr, scope)
        if node.ann:
            at = self.resolve_type(node.ann)
            self.check_compatible(at, it, node.expr,
                                  "initializer for '%s'" % node.name)
            t = at
        else:
            t = it
        scope.define(Symbol(node.name, t,
                            "let" if isinstance(node, A.LetDecl) else "var",
                            mutable=isinstance(node, A.VarDecl)))

    def check_stream_decl(self, node, scope):
        if node.name in scope.symbols:
            self.error("E_DUPLICATE", "'%s' already defined in this scope"
                       % node.name, node)
            return
        et = self.resolve_type(node.type)
        scope.define(Symbol(node.name, StreamType(et), "stream",
                            mutable=False))

    def check_super_call(self, node, scope):
        if self.current_class is None:
            self.error("E_SUPER", "super() outside a class", node)
            return
        parent = self.classes[self.current_class].parent
        if not parent:
            self.error("E_SUPER", "no parent class to call", node)
            return
        pnode = self.class_nodes[parent]
        pinit = next((m for m in pnode.members
                      if isinstance(m, A.InitDecl)), None)
        if pinit is None:
            self.error("E_SUPER", "parent %s has no init" % parent, node)
            return
        self.check_call_args(
            [(p.name, self.resolve_type(p.type)) for p in pinit.params],
            node.args, node, what="super()")

    # -- expressions --------------------------------------------------
    def check_expr(self, node, scope):
        m = "_check_" + type(node).__name__
        fn = getattr(self, m, None)
        if fn is None:
            self.error("E_INTERNAL", "no checker for %s"
                       % type(node).__name__, node)
            return ANY
        return fn(node, scope)

    def _check_IntLit(self, node, scope):
        if not (INT_MIN <= node.value <= INT_MAX):
            self.error("E_INT_RANGE",
                       "integer literal %d out of Int range "
                       "(signed 64-bit)" % node.value, node)
        return INT

    def _check_FloatLit(self, node, scope):
        return FLOAT

    def _check_StrLit(self, node, scope):
        return STRING

    def _check_BoolLit(self, node, scope):
        return BOOL

    def _check_NoneLit(self, node, scope):
        return OptionType(ANY)

    def _check_SomeExpr(self, node, scope):
        return OptionType(self.check_expr(node.expr, scope))

    def _check_OkExpr(self, node, scope):
        return ResultType(self.check_expr(node.expr, scope), ANY)

    def _check_ErrExpr(self, node, scope):
        return ResultType(ANY, self.check_expr(node.expr, scope))

    def _check_ListLit(self, node, scope):
        if not node.elems:
            return ListType(ANY)
        t0 = self.check_expr(node.elems[0], scope)
        for e in node.elems[1:]:
            t = self.check_expr(e, scope)
            self.check_compatible(t0, t, e, "list element")
        return ListType(t0)

    def _check_Var(self, node, scope):
        if node.name in BANNED_IDENTS:
            self.error("E_NO_NULL",
                       "Nova has no null — '%s' does not exist" % node.name,
                       node,
                       hint="use Option[T] with Some(x)/None instead")
            return ANY
        sym = scope.lookup(node.name)
        if sym is None:
            # maybe a class / component / interface used as value
            if node.name in self.classes:
                return ClassType(node.name)
            if node.name in self.components:
                return ComponentType(node.name)
            if node.name in self.interfaces:
                return InterfaceType(node.name)
            self.error("E_UNDEFINED", "undefined name '%s'" % node.name,
                       node)
            return ANY
        if isinstance(sym.type, FuncSig):
            self.error("E_NOT_VALUE",
                       "'%s' is a function — call it" % node.name, node)
            return ANY
        return sym.type

    def _check_This(self, node, scope):
        if self.current_class is None:
            self.error("E_THIS", "`this` outside a class", node)
            return ANY
        return ClassType(self.current_class)

    def _check_Super(self, node, scope):
        if self.current_class is None:
            self.error("E_SUPER", "`super` outside a class", node)
            return ANY
        parent = self.classes[self.current_class].parent
        if not parent:
            self.error("E_SUPER", "no parent class", node)
            return ANY
        return ClassType(parent)

    def _check_ResultVar(self, node, scope):
        if not self.in_ensures:
            self.error("E_RESULT", "`result` only valid in ensures clauses",
                       node)
            return ANY
        sym = scope.lookup("result")
        return sym.type if sym else ANY

    def _check_Binary(self, node, scope):
        lt = self.check_expr(node.left, scope)
        rt = self.check_expr(node.right, scope)
        op = node.op
        if op in ("+", "-", "*", "/", "%"):
            if lt == INT and rt == INT:
                return INT
            if lt == FLOAT and rt == FLOAT:
                return FLOAT
            if op == "+" and lt == STRING and rt == STRING:
                return STRING
            self.error("E_TYPE_MISMATCH",
                       "operator %s needs Int/Int, Float/Float or "
                       "String+String, got %s and %s" % (op, lt, rt), node,
                       hint="Nova has no implicit conversions")
            return ANY
        if op in ("==", "!="):
            if lt != rt and lt != ANY and rt != ANY:
                self.error("E_TYPE_MISMATCH",
                           "cannot compare %s with %s" % (lt, rt), node)
            return BOOL
        if op in ("<", "<=", ">", ">="):
            if (lt == INT and rt == INT) or (lt == FLOAT and rt == FLOAT):
                return BOOL
            self.error("E_TYPE_MISMATCH",
                       "operator %s needs two Ints or two Floats" % op, node)
            return BOOL
        if op in ("and", "or"):
            self.check_compatible(BOOL, lt, node.left, "left of %s" % op)
            self.check_compatible(BOOL, rt, node.right, "right of %s" % op)
            return BOOL
        self.error("E_INTERNAL", "bad operator %s" % op, node)
        return ANY

    def _check_Unary(self, node, scope):
        t = self.check_expr(node.expr, scope)
        if node.op == "not":
            self.check_compatible(BOOL, t, node.expr, "operand of not")
            return BOOL
        if node.op == "-":
            # allow -(2^63) literal for INT_MIN
            if (isinstance(node.expr, A.IntLit)
                    and node.expr.value == 2 ** 63):
                return INT
            if t == INT:
                return INT
            if t == FLOAT:
                return FLOAT
            self.error("E_TYPE_MISMATCH",
                       "unary - needs Int or Float, got %s" % t, node)
            return ANY
        return ANY

    def _check_Assign(self, node, scope):
        vt = self.check_expr(node.value, scope)
        tgt = node.target
        if isinstance(tgt, A.Var):
            sym = scope.lookup(tgt.name)
            if sym is None:
                self.error("E_UNDEFINED", "undefined name '%s'" % tgt.name,
                           tgt)
                return vt
            self._check_assignable(sym, node, scope)
            self.check_compatible(sym.type, vt, node.value,
                                  "assignment to '%s'" % tgt.name)
            return vt
        if isinstance(tgt, A.Field):
            ot = self.check_expr(tgt.obj, scope)
            if isinstance(ot, ClassType):
                fields = self.class_fields(ot.name)
                if tgt.name not in fields:
                    self.error("E_NO_FIELD", "%s has no field '%s'"
                               % (ot.name, tgt.name), tgt)
                    return vt
                ft, mut = fields[tgt.name]
                is_self_init = (self.in_init and isinstance(tgt.obj, A.This))
                if not mut and not is_self_init:
                    self.error("E_IMMUTABLE",
                               "field '%s' is let-bound (immutable)" % tgt.name,
                               tgt,
                               hint="let fields can only be assigned in init")
                self.check_compatible(ft, vt, node.value, "field assignment")
                return vt
            self.error("E_TYPE_MISMATCH", "cannot assign to field of %s" % ot,
                       tgt)
            return vt
        if isinstance(tgt, A.Index):
            ot = self.check_expr(tgt.obj, scope)
            it = self.check_expr(tgt.index, scope)
            if isinstance(ot, ListType):
                self.check_compatible(INT, it, tgt.index, "index")
                self.check_compatible(ot.elem, vt, node.value,
                                      "element assignment")
                return vt
            self.error("E_TYPE_MISMATCH", "cannot index-assign %s" % ot, tgt)
            return vt
        self.error("E_ASSIGN_TARGET", "cannot assign to this", tgt)
        return vt

    def _check_assignable(self, sym, node, scope):
        if sym.kind in ("let", "param", "prop", "cap", "result"):
            self.error("E_IMMUTABLE", "'%s' is immutable" % sym.name, node,
                       hint="it was declared with let (or is a prop/param)")
        elif sym.kind == "state" and self.effect == PURE:
            self.error("E_EFFECT",
                       "mutating state '%s' needs an effect context"
                       % sym.name, node,
                       hint="state changes belong in event handlers or "
                            "effect functions")

    def _check_Question(self, node, scope):
        if self.in_spawn:
            self.error("E_QUESTION",
                       "? cannot be used directly in a spawn block — it would "
                       "escape the task boundary", node,
                       hint="handle the Result/Option with match inside the "
                            "spawn block")
            return ANY
        t = self.check_expr(node.expr, scope)
        ret = self._current_fn_ret()
        if isinstance(t, ResultType):
            if not isinstance(ret, ResultType):
                self.error("E_QUESTION",
                           "? on Result needs an enclosing function returning "
                           "Result", node)
                return ANY
            self.check_compatible(ret.err, t.err, node.expr, "error type")
            return t.ok
        if isinstance(t, OptionType):
            if not isinstance(ret, OptionType):
                self.error("E_QUESTION",
                           "? on Option needs an enclosing function returning "
                           "Option", node)
                return ANY
            return t.elem
        self.error("E_QUESTION", "? needs Result or Option, got %s" % t,
                   node)
        return ANY

    def _current_fn_ret(self):
        return getattr(self, "_fn_ret", UNIT)

    def _check_Spawn(self, node, scope):
        # spawn { block } : Task[T] — runs on a worker thread. The block is
        # checked with the spawner's effect plus async: spawning grants the
        # right to await, and (as explicitly marked off-thread work) lifts
        # the async blocking-IO ban — but never grants new capabilities.
        self._need_effect(CONSOLE, node, "spawn")
        # Send checking (v2): only Send values may cross into a worker
        # thread. Capturing shared mutable state is E_NOT_SEND — the
        # compiler stops the data race before it exists.
        self._check_send_captures(node, scope)
        old_fx = self.effect
        old_spawn = self.in_spawn
        self.effect = self.effect | {"async"}
        self.in_spawn = True
        t = self._check_block_value(node.block, scope)
        self.effect = old_fx
        self.in_spawn = old_spawn
        return TaskType(t)

    def _check_SpawnAsync(self, node, scope):
        # spawn_async { block } : Task[T] — runs as a cooperative tasklet
        # on the event loop. Never parallel: tasklets are serialized by
        # the scheduler, so no Send checking is needed — but the async
        # blocking-IO ban applies (you're on the loop; wrap blocking
        # work in spawn { }).
        if self.in_spawn:
            self.error("E_TASKLET_CONTEXT",
                       "spawn_async { } cannot appear inside spawn { } — "
                       "the block runs on a worker thread, not the event "
                       "loop", node)
            return ANY
        self._need_effect(CONSOLE, node, "spawn_async")
        old_fx = self.effect
        old_async = self.in_async_fn
        old_sa = self.in_spawn_async
        self.effect = self.effect | {"async"}
        self.in_async_fn = True
        self.in_spawn_async = True
        t = self._check_block_value(node.block, scope)
        self.effect = old_fx
        self.in_async_fn = old_async
        self.in_spawn_async = old_sa
        return TaskType(t)

    def _check_Nursery(self, node, scope):
        # nursery { block } runs inline in the current tasklet: tasks
        # spawned inside are joined before the block exits (on every
        # exit path — the interpreter enforces it). return/? are allowed;
        # the nursery still joins on early exit.
        return self._check_block_value(node.block, scope)

    def check_context_decl(self, node, scope):
        t = self.resolve_type(node.type)
        if node.name in self.contexts:
            self.error("E_DUPLICATE",
                       "context '%s' already declared" % node.name, node)
            return
        if node.default is not None:
            dt = self.check_expr(node.default, scope)
            self.check_compatible(t, dt, node.default,
                                  "default for context '%s'" % node.name)
        self.contexts[node.name] = t

    def _check_Provide(self, node, scope):
        if node.name not in self.contexts:
            self.error("E_UNDEFINED",
                       "unknown context '%s'" % node.name, node,
                       hint="declare it first: context %s: Type = ..."
                       % node.name)
            return ANY
        t = self.contexts[node.name]
        vt = self.check_expr(node.value, scope)
        self.check_compatible(t, vt, node.value,
                              "provided value for context '%s'" % node.name)
        return self._check_block_value(node.block, scope)

    def _check_Consume(self, node, scope):
        if node.name not in self.contexts:
            self.error("E_UNDEFINED",
                       "unknown context '%s'" % node.name, node,
                       hint="declare it first: context %s: Type = ..."
                       % node.name)
            return ANY
        return self.contexts[node.name]

    # -- Send checking ------------------------------------------------
    def _is_send(self, t):
        """Can a value of this type safely cross a thread boundary?

        Primitives, immutable aggregates of Send types, and thread-safe
        handles are Send. Anything with interior mutability (List,
        class instances with mutable fields, components with state,
        closures) is not."""
        if isinstance(t, PrimType):
            return t.name in ("Int", "Float", "String", "Bool", "Unit")
        if isinstance(t, OptionType):
            return self._is_send(t.elem)
        if isinstance(t, ResultType):
            return self._is_send(t.ok) and self._is_send(t.err)
        if isinstance(t, TaskType):
            return self._is_send(t.elem)
        if isinstance(t, (StreamType, SubscriptionType, ModuleType)):
            return True  # thread-safe handles / immutable namespaces
        if isinstance(t, MessageType):
            return self._is_send(t.elem)
        if isinstance(t, _Cap):
            return False  # capabilities stay with their granting thread
        if isinstance(t, FuncSig):
            # Nova has no nested functions or closures (E_NESTED_FN), so
            # a fn reference is immutable code — always Send.
            return True
        if isinstance(t, ClassType):
            # Class instance: Send iff every field is immutable and Send.
            # Data classes (all-let fields) are Send; a single `var`
            # field makes the whole instance non-Send.
            fields = self.class_fields(t.name)
            for _fname, (ft, mutable) in fields.items():
                if mutable or not self._is_send(ft):
                    return False
            return True
        if isinstance(t, InstanceType):
            return False  # component instances hold mutable state
        return False  # ListType, FuncSig, InterfaceType, ANY: conservative

    def _block_free_vars(self, block):
        """Names referenced but not bound inside the block.

        Returns (free_refs, free_assigns): lists of (name, node). `this`
        is reported under the pseudo-name 'this'."""
        import dataclasses
        bound_stack = [set()]
        refs = []
        assigned = []

        def is_bound(name):
            return any(name in s for s in reversed(bound_stack))

        def visit(n):
            if n is None:
                return
            if isinstance(n, list):
                for x in n:
                    visit(x)
                return
            if not isinstance(n, A.Node):
                return
            if isinstance(n, (A.LetDecl, A.VarDecl)):
                visit(n.expr)
                if n.ann:
                    visit(n.ann)
                bound_stack[-1].add(n.name)
                return
            if isinstance(n, A.FnDecl):
                bound_stack.append({p.name for p in n.params})
                visit(n.body)
                bound_stack.pop()
                bound_stack[-1].add(n.name)
                return
            if isinstance(n, A.MethodDecl):
                bound_stack.append({p.name for p in n.params})
                visit(n.body)
                bound_stack.pop()
                return
            if isinstance(n, A.InitDecl):
                bound_stack.append({p.name for p in n.params})
                visit(n.body)
                bound_stack.pop()
                return
            if isinstance(n, A.ForStmt):
                visit(n.iter)
                bound_stack.append({n.var})
                visit(n.body)
                bound_stack.pop()
                return
            if isinstance(n, A.Block):
                bound_stack.append(set())
                for s in n.stmts:
                    visit(s)
                bound_stack.pop()
                return
            if isinstance(n, A.Assign):
                if isinstance(n.target, A.Var):
                    assigned.append((n.target.name, n.target))
                    refs.append((n.target.name, n.target))
                else:
                    visit(n.target)
                visit(n.value)
                return
            if isinstance(n, A.Var):
                refs.append((n.name, n))
                return
            if isinstance(n, A.This):
                refs.append(("this", n))
                return
            for f in dataclasses.fields(n):
                v = getattr(n, f.name)
                if isinstance(v, A.Node) or isinstance(v, list):
                    visit(v)

        visit(block)
        free_refs = [(nm, nd) for nm, nd in refs if not is_bound(nm)]
        free_assigns = [(nm, nd) for nm, nd in assigned if not is_bound(nm)]
        return free_refs, free_assigns

    def _check_send_captures(self, node, scope):
        free_refs, free_assigns = self._block_free_vars(node.block)
        for name, nd in free_assigns:
            self.error("E_NOT_SEND",
                       "cannot assign to captured '%s' inside spawn { } — "
                       "the variable is shared with another thread" % name,
                       nd,
                       hint="compute the value inside the block and return "
                            "it, or use message passing")
        seen = set()
        for name, nd in free_refs:
            if name in seen:
                continue
            seen.add(name)
            if name == "this":
                if self.current_class is None:
                    continue
                t = InstanceType(self.current_class)
                what = "'this'"
            else:
                sym = scope.lookup(name)
                if sym is None:
                    continue  # E_UNDEFINED is reported by normal checking
                t = sym.type
                what = "'%s'" % name
            if not self._is_send(t):
                self.error("E_NOT_SEND",
                           "cannot capture %s of type %s in spawn { } — "
                           "it is not Send (shared mutable state would "
                           "race)" % (what, t), nd,
                           hint="capture only immutable/Send values; move "
                                "mutation inside the block or use a stream")

    def _check_Call(self, node, scope):
        func = node.func
        if isinstance(func, A.Var):
            name = func.name
            if name in ("IO", "Net"):
                self.error("E_CAPABILITY",
                           "capabilities cannot be constructed — receive "
                           "them as parameters", func,
                           hint="add e.g. `cap: %s` to the function signature"
                           % name)
                return ANY
            sym = scope.lookup(name)
            if sym is not None and isinstance(sym.type, FuncSig):
                sig = sym.type
                self._check_effect(sig, node)
                self.check_call_args(
                    sig.params, node.args, node, what="function " + name)
                return sig.ret
            if name in self.classes:
                return self._check_construction(name, node, scope)
            if name in self.components:
                comp = self.components[name]
                params = [(p.name, self.resolve_type(p.type))
                          for p in comp.params]
                self.check_call_args(params, node.args, node,
                                     what="component " + name)
                return InstanceType(name)
            if name in ("print", "str", "int", "len", "assert", "render",
                        "readFile", "writeFile", "fetch", "prompt",
                        "secret", "sleep"):
                return self._check_builtin(name, node, scope)
            if sym is None:
                self.error("E_UNDEFINED", "undefined name '%s'" % name, func)
            else:
                self.error("E_NOT_CALLABLE", "'%s' is not callable" % name,
                           func)
            return ANY
        if isinstance(func, A.Field):
            # module.attr(...)
            ot = self.check_expr(func.obj, scope)
            if isinstance(ot, ModuleType):
                return self._check_module_member_call(ot.name, func.name,
                                                      node, scope)
            self.error("E_TYPE_MISMATCH", "cannot call %s.%s"
                       % (ot, func.name), func)
            return ANY
        self.error("E_NOT_CALLABLE", "not callable", func)
        return ANY

    def _check_effect(self, sig, node):
        # Async discipline: blocking IO directly inside an async fn would
        # stall the event loop. The compiler says so (E_ASYNC_BLOCKING) —
        # wrap the call in spawn { } to mark it explicitly off-thread.
        if (self.in_async_fn and not self.in_spawn
                and (sig.effect & {"io", "net"})):
            self.error("E_ASYNC_BLOCKING",
                       "blocking IO '%s' is not allowed directly in an async "
                       "fn" % sig.name, node,
                       hint="wrap it in spawn { ... } to run it off the "
                            "event loop, or drop `async`")
            return
        # "async" in the callee's set is not a barrier: it marks the
        # function as await-capable, it doesn't restrict callers.
        need = sig.effect - {"async"}
        if not need.issubset(self.effect):
            self.error("E_EFFECT",
                       "calling '%s' needs effect %s" % (
                           sig.name, sorted(need) or ["pure"]),
                       node,
                       hint="mark the caller `effect`, or move the call")

    def _check_construction(self, name, node, scope):
        cls = self.class_nodes[name]
        info = self.classes[name]
        if info.is_data:
            fields = self.class_fields(name)
            params = [(fn, ft) for fn, (ft, _) in fields.items()]
        else:
            init = next((m for m in cls.members
                         if isinstance(m, A.InitDecl)), None)
            params = ([(p.name, self.resolve_type(p.type))
                       for p in init.params] if init else [])
        self.check_call_args(params, node.args, node, what="init of " + name)
        return ClassType(name)

    def _check_builtin(self, name, node, scope):
        args = node.args
        vals = [self.check_expr(a[1], scope) for a in args]
        if (name in BLOCKING_BUILTINS and self.in_async_fn
                and not self.in_spawn):
            self.error("E_ASYNC_BLOCKING",
                       "blocking IO '%s' is not allowed directly in an async "
                       "fn" % name, node,
                       hint="wrap it in spawn { ... } to run it off the "
                            "event loop, or drop `async`")
            return ANY
        if name == "print":
            self._need_effect(CONSOLE, node, "print")
            return UNIT
        if name == "str":
            if len(vals) != 1:
                self.error("E_ARGC", "str takes 1 argument", node)
            return STRING
        if name == "int":
            if len(vals) != 1:
                self.error("E_ARGC", "int takes 1 argument", node)
            elif vals:
                self.check_compatible(FLOAT, vals[0], args[0][1], "int()")
            return INT
        if name == "len":
            if len(vals) != 1:
                self.error("E_ARGC", "len takes 1 argument", node)
            elif vals and not isinstance(vals[0], ListType):
                self.error("E_TYPE_MISMATCH",
                           "len needs a List, got %s" % vals[0], node)
            return INT
        if name == "assert":
            if len(vals) not in (1, 2):
                self.error("E_ARGC", "assert takes 1 or 2 arguments", node)
            elif vals:
                self.check_compatible(BOOL, vals[0], args[0][1], "assert")
            return UNIT
        if name == "render":
            self._need_effect(CONSOLE, node, "render")
            if len(vals) != 1:
                self.error("E_ARGC", "render takes 1 argument", node)
            elif vals:
                if not isinstance(vals[0], InstanceType):
                    self.error("E_TYPE_MISMATCH",
                               "render needs a component instance, got %s"
                               % vals[0], node)
                return vals[0]
            return ANY
        if name in ("readFile", "writeFile"):
            self._need_effect(IO_EFF, node, name)
            want = [STRING, _Cap("IO")] if name == "readFile" else \
                [STRING, STRING, _Cap("IO")]
            self._check_arg_types(name, want, vals, args, node)
            return (ResultType(STRING, STRING) if name == "readFile"
                    else ResultType(UNIT, STRING))
        if name == "fetch":
            self._need_effect(NET_EFF, node, name)
            self._check_arg_types(name, [STRING, _Cap("Net")], vals, args,
                                  node)
            return ResultType(STRING, STRING)
        if name == "prompt":
            if not self.in_ai_body:
                self.error("E_AI_PROMPT",
                           "prompt() is only valid inside an ai fn body",
                           node)
            if len(vals) != 1:
                self.error("E_ARGC", "prompt takes 1 argument", node)
            elif vals:
                self.check_compatible(STRING, vals[0], args[0][1], "prompt")
            return UNIT
        if name == "secret":
            # secret(name, io) — capability-gated secret store. v1 resolves
            # from NOVA_SECRET_<NAME> env vars; never from source code.
            self._need_effect(IO_EFF, node, name)
            self._check_arg_types(name, [STRING, _Cap("IO")], vals, args,
                                  node)
            return ResultType(STRING, STRING)
        if name == "sleep":
            # sleep(seconds) — cooperative park on the event loop's timer.
            # In a spawn { } worker thread it blocks the thread instead.
            self._need_effect(CONSOLE, node, "sleep")
            if len(vals) != 1:
                self.error("E_ARGC", "sleep takes 1 argument", node)
            elif vals and not isinstance(vals[0], (PrimType,)):
                self.error("E_TYPE_MISMATCH",
                           "sleep needs a number, got %s" % vals[0], node)
            elif vals and vals[0].name not in ("Int", "Float"):
                self.error("E_TYPE_MISMATCH",
                           "sleep needs Int or Float, got %s" % vals[0],
                           node)
            return UNIT
        return ANY

    def _need_effect(self, eff, node, what):
        if not eff.issubset(self.effect):
            self.error("E_EFFECT", "'%s' is an effect" % what, node,
                       hint="call it from an effect fn or top-level code")

    def _check_arg_types(self, name, want, vals, args, node):
        if len(vals) != len(want):
            self.error("E_ARGC", "%s takes %d arguments" % (name, len(want)),
                       node)
            return
        for w, v, a in zip(want, vals, args):
            self.check_compatible(w, v, a[1], "argument of %s" % name)

    def check_call_args(self, params, args, node, what):
        # params: [(name, Type)]; args: [(name|None, expr)]
        seen = {}
        pos = 0
        scope = self._scope_stack[-1] if self._scope_stack else \
            self.global_scope
        for aname, aexpr in args:
            at = self.check_expr(aexpr, scope)
            if aname is not None:
                match = next((i for i, (pn, _) in enumerate(params)
                              if pn == aname), None)
                if match is None:
                    self.error("E_ARGC", "%s has no parameter '%s'"
                               % (what, aname), aexpr)
                    continue
                pname, ptype = params[match]
            else:
                if pos >= len(params):
                    self.error("E_ARGC", "%s takes %d argument(s)"
                               % (what, len(params)), node)
                    continue
                pname, ptype = params[pos]
                pos += 1
            if pname in seen:
                self.error("E_ARGC", "duplicate argument '%s'" % pname,
                           aexpr)
            seen[pname] = True
            self.check_compatible(ptype, at, aexpr,
                                  "argument '%s' of %s" % (pname, what))
        missing = [pn for pn, _ in params if pn not in seen]
        if missing:
            self.error("E_ARGC", "%s missing arguments: %s"
                       % (what, ", ".join(missing)), node)

    def _check_MethodCall(self, node, scope):
        ot = self.check_expr(node.obj, scope)
        if isinstance(ot, ListType) and node.name == "push":
            if len(node.args) != 1 or node.args[0][0] is not None:
                self.error("E_ARGC", "push takes 1 positional argument",
                           node)
                return UNIT
            at = self.check_expr(node.args[0][1], scope)
            self.check_compatible(ot.elem, at, node.args[0][1],
                                  "push element")
            return UNIT
        if isinstance(ot, (OptionType, ResultType)) and node.name in (
                "unwrap", "expect"):
            if node.name == "expect":
                if len(node.args) != 1:
                    self.error("E_ARGC", "expect takes 1 argument", node)
            elif node.args:
                self.error("E_ARGC", "unwrap takes no arguments", node)
            return ot.elem if isinstance(ot, OptionType) else ot.ok
        if isinstance(ot, TaskType) and node.name == "await":
            if self.effect == PURE:
                self.error("E_EFFECT",
                           "await blocks the thread — it needs an effect "
                           "context", node,
                           hint="await from an async fn, an effect fn, or "
                                "top-level code")
            if node.args:
                self.error("E_ARGC", "await takes no arguments", node)
            return ot.elem
        if isinstance(ot, StreamType) and node.name == "publish":
            self._need_effect(CONSOLE, node, "publish")
            if len(node.args) == 1 and node.args[0][0] is None:
                at = self.check_expr(node.args[0][1], scope)
                self.check_compatible(ot.elem, at, node.args[0][1],
                                      "publish value")
            elif (len(node.args) == 2 and node.args[0][0] == "key"
                    and node.args[1][0] == "value"):
                kt = self.check_expr(node.args[0][1], scope)
                self.check_compatible(STRING, kt, node.args[0][1], "key")
                vt = self.check_expr(node.args[1][1], scope)
                self.check_compatible(ot.elem, vt, node.args[1][1],
                                      "publish value")
            else:
                self.error("E_ARGC",
                           "publish(value) or publish(key: String, value)",
                           node)
            return UNIT
        if isinstance(ot, StreamType) and node.name == "subscribe":
            self._need_effect(CONSOLE, node, "subscribe")
            self.check_call_args([("group", STRING)], node.args, node,
                                 what="subscribe")
            return SubscriptionType(ot.elem)
        if isinstance(ot, SubscriptionType) and node.name == "poll":
            self._need_effect(CONSOLE, node, "poll")
            if node.args:
                self.error("E_ARGC", "poll takes no arguments", node)
            return OptionType(MessageType(ot.elem))
        if isinstance(ot, SubscriptionType) and node.name == "commit":
            self._need_effect(CONSOLE, node, "commit")
            if len(node.args) != 1 or node.args[0][0] is not None:
                self.error("E_ARGC",
                           "commit takes 1 positional argument (the message)",
                           node)
            else:
                at = self.check_expr(node.args[0][1], scope)
                self.check_compatible(MessageType(ot.elem), at,
                                      node.args[0][1], "commit message")
            return UNIT
        if isinstance(ot, InstanceType) and node.name == "fire":
            self._need_effect(CONSOLE, node, "fire")
            if len(node.args) != 1:
                self.error("E_ARGC", "fire takes 1 argument", node)
            elif node.args[0][0] is None:
                at = self.check_expr(node.args[0][1], scope)
                self.check_compatible(STRING, at, node.args[0][1],
                                      "event name")
            return UNIT
        if isinstance(ot, ClassType):
            methods = self.class_methods(ot.name)
            if node.name in methods:
                m = methods[node.name]
                params = [(p.name, self.resolve_type(p.type))
                          for p in m.params]
                ret = self.resolve_type(m.ret) if m.ret else UNIT
                self._check_effect(
                    FuncSig(m.name, params, ret, EFFECT_OF[m.effect]), node)
                self.check_call_args(params, node.args, node,
                                     what="method " + node.name)
                return ret
            self.error("E_NO_METHOD", "%s has no method '%s'"
                       % (ot.name, node.name), node)
            return ANY
        if isinstance(ot, InterfaceType):
            idecl = self.interfaces.get(ot.name)
            if idecl is None:
                self.error("E_UNDEFINED",
                           "interface '%s' not found" % ot.name, node)
                return ANY
            m = next((x for x in idecl.methods if x.name == node.name),
                     None)
            if m is None:
                self.error("E_NO_METHOD", "%s has no method '%s'"
                           % (ot.name, node.name), node)
                return ANY
            params = [(p.name, self.resolve_type(p.type))
                      for p in m.params]
            ret = self.resolve_type(m.ret) if m.ret else UNIT
            self._check_effect(
                FuncSig(m.name, params, ret, EFFECT_OF[m.effect]), node)
            self.check_call_args(params, node.args, node,
                                 what="method " + node.name)
            return ret
        if isinstance(ot, ModuleType):
            return self._check_module_member_call(ot.name, node.name,
                                                  node, scope)
        self.error("E_NO_METHOD", "type %s has no method '%s'" % (ot,
                                                                 node.name),
                   node)
        return ANY

    def _check_module_member_call(self, modname, member, node, scope):
        """mod.name(...) — resolves functions, classes, components."""
        exports = self.modules.get(modname, {})
        sym = exports.get(member)
        if sym is None:
            if member.startswith("_"):
                self.error("E_IMPORT",
                           "'%s' is private to module %s" % (member, modname),
                           node)
            else:
                self.error("E_UNDEFINED",
                           "module %s has no '%s'" % (modname, member), node)
            return ANY
        t = sym.type
        if isinstance(t, FuncSig):
            self._check_effect(t, node)
            self.check_call_args(t.params, node.args, node,
                                 what="%s.%s" % (modname, member))
            return t.ret
        if isinstance(t, ClassType):
            return self._check_construction(t.name, node, scope)
        if isinstance(t, ComponentType):
            comp = self.components[t.name]
            params = [(p.name, self.resolve_type(p.type))
                      for p in comp.params]
            self.check_call_args(params, node.args, node,
                                 what="component " + t.name)
            return InstanceType(t.name)
        self.error("E_NOT_CALLABLE", "'%s' is not callable" % member, node)
        return ANY

    def _check_Field(self, node, scope):
        ot = self.check_expr(node.obj, scope)
        if isinstance(ot, MessageType):
            if node.name == "topic":
                return STRING
            if node.name == "offset":
                return INT
            if node.name == "key":
                return OptionType(STRING)
            if node.name == "value":
                return ot.elem
            self.error("E_NO_FIELD", "Message has no field '%s'" % node.name,
                       node,
                       hint="fields: topic, offset, key: Option[String], "
                            "value")
            return ANY
        if isinstance(ot, ClassType):
            fields = self.class_fields(ot.name)
            if node.name in fields:
                return fields[node.name][0]
            self.error("E_NO_FIELD", "%s has no field '%s'"
                       % (ot.name, node.name), node,
                       hint="fields are declared with let/var in the class")
            return ANY
        if isinstance(ot, ModuleType):
            exports = self.modules.get(ot.name, {})
            sym = exports.get(node.name)
            if sym is None:
                if node.name.startswith("_"):
                    self.error("E_IMPORT",
                               "'%s' is private to module %s"
                               % (node.name, ot.name), node)
                else:
                    self.error("E_UNDEFINED",
                               "module %s has no '%s'" % (ot.name, node.name),
                               node)
                return ANY
            if isinstance(sym.type, FuncSig):
                self.error("E_NOT_VALUE",
                           "'%s' is a function value — Nova has no "
                           "first-class functions in v1" % func.name, func)
                return ANY
            return sym.type
        self.error("E_NO_FIELD", "type %s has no field '%s'" % (ot,
                                                                node.name),
                   node)
        return ANY

    def _check_Index(self, node, scope):
        ot = self.check_expr(node.obj, scope)
        it = self.check_expr(node.index, scope)
        if isinstance(ot, ListType):
            self.check_compatible(INT, it, node.index, "index")
            return ot.elem
        self.error("E_TYPE_MISMATCH", "cannot index %s" % ot, node)
        return ANY

    def _check_IfExpr(self, node, scope):
        ct = self.check_expr(node.cond, scope)
        self.check_compatible(BOOL, ct, node.cond, "if condition")
        # check branches as blocks but take last-expr type
        tt = self._check_block_value(node.then_b, scope)
        et = self._check_block_value(node.else_b, scope)
        self.check_compatible(tt, et, node.else_b, "else branch")
        return tt

    def _check_block_value(self, block, scope):
        sub = Scope(scope)
        t = UNIT
        for s in block.stmts:
            if isinstance(s, A.ExprStmt):
                t = self.check_expr(s.expr, sub)
            else:
                self.check_stmt(s, sub)
                t = UNIT
        return t

    def _check_Match(self, node, scope):
        st = self.check_expr(node.scrutinee, scope)
        if not node.arms:
            self.error("E_NON_EXHAUSTIVE", "match needs at least one arm",
                       node)
            return ANY
        arm_types = []
        covered = set()
        has_wild = False
        for arm in node.arms:
            sub = Scope(scope)
            kind = self.check_pattern(arm.pattern, st, sub)
            covered.add(kind)
            if kind == "wild":
                has_wild = True
            # check arm expr with pattern bindings in scope
            saved_stack = self._scope_stack
            self._scope_stack = self._scope_stack + [sub]
            arm_types.append(self.check_expr(arm.expr, sub))
            self._scope_stack = saved_stack
        # exhaustiveness
        if isinstance(st, OptionType):
            need = {"some", "none"}
        elif isinstance(st, ResultType):
            need = {"ok", "err"}
        elif st == BOOL:
            need = {"lit-True", "lit-False"}
        else:
            need = set()
        if not has_wild:
            if need and not need.issubset(covered):
                self.error("E_NON_EXHAUSTIVE",
                           "match on %s is not exhaustive" % st, node,
                           hint="add arms for %s, or a `_` fallback"
                           % ", ".join(sorted(need - covered)))
            elif not need:
                self.error("E_NON_EXHAUSTIVE",
                           "match needs a `_` fallback arm", node,
                           hint="Nova matches must be exhaustive")
        t0 = arm_types[0]
        for arm, t in zip(node.arms[1:], arm_types[1:]):
            self.check_compatible(t0, t, arm.expr, "match arm")
        return t0

    def check_pattern(self, pat, st, scope):
        """Returns coverage kind string. Binds names into scope."""
        if isinstance(pat, A.PatSome):
            if not isinstance(st, OptionType):
                self.error("E_PATTERN", "Some(x) needs Option, got %s" % st,
                           pat)
                return "some"
            scope.define(Symbol(pat.name, st.elem, "let", mutable=False))
            return "some"
        if isinstance(pat, A.PatNone):
            if not isinstance(st, OptionType):
                self.error("E_PATTERN", "None needs Option, got %s" % st,
                           pat)
            return "none"
        if isinstance(pat, A.PatOk):
            if not isinstance(st, ResultType):
                self.error("E_PATTERN", "Ok(x) needs Result, got %s" % st,
                           pat)
                return "ok"
            scope.define(Symbol(pat.name, st.ok, "let", mutable=False))
            return "ok"
        if isinstance(pat, A.PatErr):
            if not isinstance(st, ResultType):
                self.error("E_PATTERN", "Err(x) needs Result, got %s" % st,
                           pat)
                return "err"
            scope.define(Symbol(pat.name, st.err, "let", mutable=False))
            return "err"
        if isinstance(pat, A.PatLit):
            lt = {int: INT, float: FLOAT, str: STRING, bool: BOOL}.get(
                type(pat.value), ANY)
            self.check_compatible(st, lt, pat, "pattern")
            return "lit-%r" % (pat.value,)
        if isinstance(pat, A.PatWild):
            return "wild"
        if isinstance(pat, A.PatVar):
            scope.define(Symbol(pat.name, st, "let", mutable=False))
            return "wild"
        return "wild"


class _Cap(Type):
    """Capability type: IO / Net. Uninhabited by user code."""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, _Cap) and other.name == self.name

    def __hash__(self):
        return hash(("cap", self.name))

    def __repr__(self):
        return self.name


def _is_event(name):
    return name.startswith("on") and len(name) > 2 and name[2].isupper()


def check_source(source, filename, basedir, is_entry=True):
    """Parse + check. Returns (exports, diagnostics)."""
    from .lexer import Lexer, LexError
    from .parser import Parser, ParseError, JsxError
    try:
        toks = Lexer(source, filename).tokenize()
    except LexError as e:
        return {}, [Diagnostic("E_LEXER", str(e), e.line, e.col)]
    try:
        prog = Parser(toks, source, filename).parse_program()
    except ParseError as e:
        return {}, [Diagnostic("E_SYNTAX", str(e), e.line, e.col, e.hint)]
    except JsxError as e:
        return {}, [Diagnostic("E_SYNTAX", "JSX: " + str(e), e.line, e.col)]
    checker = Checker(basedir, filename, is_entry=is_entry)
    exports = checker.check_program(prog)
    return exports, checker.errors
