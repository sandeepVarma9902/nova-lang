# interpreter.py — Nova tree-walking interpreter.
#
# Runs only after the checker approves a program, so most static errors are
# already ruled out. Runtime still enforces: contracts, checked integer
# arithmetic (no silent wraparound), capability discipline, and effect
# boundaries for dynamic values.

import os
import time
from . import ast as A
from .loop import (EventLoop, Tasklet, TaskletCancelled, DeadlockError,
                   current_tasklet, SleepAwaitable, TaskAwaitable,
                   TaskletAwaitable)
from .types import (INT, FLOAT, STRING, BOOL, UNIT, ANY, ListType, OptionType,
                    ResultType, ClassType, InstanceType, ClassInfo, JSON, DB)
from .checker import (PURE, CONSOLE, IO_EFF, NET_EFF, TOP_EFF, EFFECT_OF,
                      FuncSig, _Cap, Checker)
from .web import VRouter, VRequest, VResponse
from .db import VDb, VQuery

INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1


class NovaError(Exception):
    def __init__(self, code, message, line=0, col=0, hint=""):
        super().__init__(message)
        self.code = code
        self.line = line
        self.col = col
        self.hint = hint


# -- blocking syscalls -------------------------------------------------
# Pure functions of their arguments: safe to offload to the loop's IO
# pool when called from a tasklet (genuine async IO — the tasklet parks
# instead of stalling the scheduler). Called directly on worker threads.
def _do_readFile(path):
    try:
        with open(path) as f:
            return VResult(STRING, STRING, True, f.read())
    except OSError as e:
        return VResult(STRING, STRING, False, str(e))


def _do_writeFile(path, content):
    try:
        with open(path, "w") as f:
            f.write(content)
        return VResult(STRING, STRING, True, VUNIT)
    except OSError as e:
        return VResult(STRING, STRING, False, str(e))


def _do_fetch(url):
    try:
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "nova-lang/0.2"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return VResult(STRING, STRING, True,
                           r.read().decode("utf-8", "replace"))
    except Exception as e:
        return VResult(STRING, STRING, False, str(e))


class ReturnExc(Exception):
    def __init__(self, value):
        self.value = value


class EarlyReturn(Exception):
    """The `?` operator propagating Ok/Err/None out of a function."""
    def __init__(self, value):
        self.value = value


# ---------------------------------------------------------------- values ----
class VUnit:
    _inst = None

    def __repr__(self):
        return "()"


VUNIT = VUnit()


class VUnset:
    def __repr__(self):
        return "<unset>"


VUNSET = VUnset()


class VList:
    def __init__(self, elem_type, items):
        self.elem_type = elem_type
        self.items = items


class VOption:
    def __init__(self, inner, value):
        self.inner = inner
        self.value = value  # None means None


class VResult:
    def __init__(self, ok_t, err_t, is_ok, value):
        self.ok_t = ok_t
        self.err_t = err_t
        self.is_ok = is_ok
        self.value = value


class VCapability:
    def __init__(self, name):
        self.name = name


class VJson:
    """A parsed JSON document (dict | list | str | int | float | bool)."""

    def __init__(self, value):
        self.value = value


class VVector:
    """A fixed-dimension embedding vector."""

    def __init__(self, dim, items):
        self.dim = dim
        self.items = items  # [float], len == dim


class VModule:
    def __init__(self, name, attrs):
        self.name = name
        self.attrs = attrs


class VTask:
    """A spawned concurrent unit: either a worker-pool future (spawn { })
    or an event-loop tasklet (spawn_async { })."""
    def __init__(self, future=None, interp=None, tasklet=None):
        self.future = future      # concurrent.futures.Future | None
        self.interp = interp      # task-local Interpreter (pool tasks)
        self.tasklet = tasklet    # loop.Tasklet (spawn_async) | None
        self.merged = False       # task output merged into the awaiting output
        self.awaited = False


class VStream:
    """A named pub/sub topic handle."""
    def __init__(self, topic, provider):
        self.topic = topic
        self.provider = provider

    def publish(self, key, value):
        self.provider.publish(self.topic, key, value)


class VSubscription:
    """A consumer-group subscription on a stream."""
    def __init__(self, stream, group):
        self.stream = stream
        self.group = group


class VMessage:
    """One consumed record."""
    def __init__(self, topic, offset, key, value):
        self.topic = topic
        self.offset = offset
        self.key = key
        self.value = value


class VInstance:
    def __init__(self, classdef, fields):
        self.classdef = classdef
        self.fields = fields  # name -> value


class VCompInstance:
    def __init__(self, compdef, props, state):
        self.compdef = compdef
        self.props = props      # name -> value
        self.state = state      # name -> Cell
        self.tree = None        # last rendered VNode


class FuncValue:
    def __init__(self, decl, closure, bound_this=None):
        self.decl = decl        # FnDecl | MethodDecl
        self.closure = closure
        self.bound_this = bound_this


class AiFuncValue:
    def __init__(self, decl, closure):
        self.decl = decl
        self.closure = closure


class RuntimeClass:
    def __init__(self, name, decl, parent, fields, methods, init):
        self.name = name
        self.decl = decl
        self.parent = parent        # RuntimeClass | None
        self.fields = fields        # {name: (type, mutable)}
        self.methods = methods      # {name: MethodDecl}
        self.init = init            # InitDecl | None


class VNode:
    def __init__(self, tag, attrs, handlers, children):
        self.tag = tag
        self.attrs = attrs          # [(name, value|None)]
        self.handlers = handlers    # {name: expr}
        self.children = children    # [VNode | str]


class Cell:
    __slots__ = ("value", "mutable", "kind")

    def __init__(self, value, mutable, kind):
        self.value = value
        self.mutable = mutable
        self.kind = kind


class Env:
    def __init__(self, parent=None, state_cells=None):
        self.parent = parent
        self.cells = {}
        self.state_cells = state_cells

    def define(self, name, value, mutable, kind):
        self.cells[name] = Cell(value, mutable, kind)

    def lookup_cell(self, name):
        if name in self.cells:
            return self.cells[name]
        if self.state_cells is not None and name in self.state_cells:
            return self.state_cells[name]
        if self.parent is not None:
            return self.parent.lookup_cell(name)
        return None


def _json_compact(v):
    import json as _json
    try:
        s = _json.dumps(v, separators=(",", ":"))
    except (TypeError, ValueError):
        s = "?"
    return s[:60]


def nova_to_json(v):
    """Convert a Nova value to a JSON-encodable Python value."""
    import json as _json
    if v is VUNIT:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, str):
        return v
    if isinstance(v, VList):
        return [nova_to_json(x) for x in v.items]
    if isinstance(v, VOption):
        return None if v.value is None else nova_to_json(v.value)
    if isinstance(v, VResult):
        return {"ok": nova_to_json(v.value)} if v.is_ok else \
            {"err": nova_to_json(v.value)}
    if isinstance(v, VJson):
        return v.value
    if isinstance(v, VVector):
        return v.items
    if isinstance(v, VInstance):
        return {k: nova_to_json(x) for k, x in v.fields.items()}
    raise TypeError("cannot serialize %s to JSON" % type(v).__name__)


def json_dumps(v):
    import json as _json
    return _json.dumps(nova_to_json(v), separators=(",", ":"))


def nova_repr(v):
    if v is VUNIT:
        return "()"
    if v is VUNSET:
        return "<unset>"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, VList):
        return "[%s]" % ", ".join(nova_repr(x) for x in v.items)
    if isinstance(v, VOption):
        return "None" if v.value is None else "Some(%s)" % nova_repr(v.value)
    if isinstance(v, VResult):
        tag = "Ok" if v.is_ok else "Err"
        return "%s(%s)" % (tag, nova_repr(v.value))
    if isinstance(v, VInstance):
        inner = ", ".join("%s: %s" % (k, nova_repr(x))
                          for k, x in v.fields.items())
        return "%s(%s)" % (v.classdef.name, inner)
    if isinstance(v, VCompInstance):
        return "<%s instance>" % v.compdef.name
    if isinstance(v, VModule):
        return "<module %s>" % v.name
    if isinstance(v, VTask):
        return "<task>"
    if isinstance(v, VStream):
        return "<stream %s>" % v.topic
    if isinstance(v, VSubscription):
        return "<subscription %s:%s>" % (v.stream.topic, v.group)
    if isinstance(v, VMessage):
        return "Message(topic: %s, offset: %s, key: %s, value: %s)" % (
            nova_repr(v.topic), nova_repr(v.offset),
            nova_repr(v.key), nova_repr(v.value))
    if isinstance(v, VCapability):
        return "<%s capability>" % v.name
    if isinstance(v, VJson):
        return "<json %s>" % _json_compact(v.value)
    if isinstance(v, VVector):
        return "<vector[%d]>" % v.dim
    if isinstance(v, VRouter):
        return "<router %d routes>" % len(v.routes)
    if isinstance(v, VRequest):
        return "<request %s %s>" % (v.method, v.path)
    if isinstance(v, VResponse):
        return "<response %d>" % v.status
    if isinstance(v, VDb):
        return "<db>"
    if isinstance(v, VQuery):
        return "<query %s>" % v.table
    if isinstance(v, FuncValue):
        return "<fn %s>" % v.decl.name
    if isinstance(v, AiFuncValue):
        return "<ai fn %s>" % v.decl.name
    if isinstance(v, RuntimeClass):
        return "<class %s>" % v.name
    return str(v)


# ------------------------------------------------------------ interpreter ----
class Interpreter:
    def __init__(self, basedir, provider=None, is_entry=True,
                 threading="auto", executor=None, stream_provider=None):
        import threading as _th
        self._tls = _th.local()  # tasklet-local execution state (see below)
        self.basedir = basedir
        self.is_entry = is_entry
        self.output = []
        self.classes = {}       # name -> RuntimeClass
        self.components = {}    # name -> ComponentDecl
        self.modules = {}       # name -> VModule
        self._fx_init = TOP_EFF if is_entry else CONSOLE
        self._tls.fx = self._fx_init
        self.global_env = Env()
        self._tls.ai_prompt_parts = []
        self._tls.in_ai_body = False
        self.provider = provider
        self._loaded_modules = set()
        self._module_lock = _th.Lock()  # shared with task clones
        self.loop = None        # EventLoop, installed by run()
        self.contexts = {}      # name -> default Nova value
        # -- platform: concurrency + streams --------------------------
        # threading: "single" (inline, deterministic), "multi"/"auto"
        # (worker thread pool). spawn { } opts into real parallelism;
        # spawn_async { } and async fns run as cooperative tasklets on
        # the event loop, serialized by the scheduler.
        self.threading = threading
        if executor is None and threading != "single":
            import concurrent.futures as _cf
            import os as _os
            workers = max(2, _os.cpu_count() or 2)
            executor = _cf.ThreadPoolExecutor(max_workers=workers,
                                              thread_name_prefix="nova-task")
        self.executor = executor
        if stream_provider is None:
            from .streams import select_stream_provider
            stream_provider = select_stream_provider()
        self.stream_provider = stream_provider
        self._tls.tasks = []    # outstanding VTasks spawned by this tasklet

    # -- tasklet-local state ----------------------------------------
    # Tasklets share one Interpreter but run on separate threads,
    # serialized by the event loop. Effect context, outstanding tasks,
    # nursery/context stacks and AI state are per-tasklet; code and
    # output are shared.
    @property
    def fx(self):
        if not hasattr(self._tls, "fx"):
            self._tls.fx = self._fx_init
        return self._tls.fx

    @fx.setter
    def fx(self, v):
        self._tls.fx = v

    @property
    def tasks(self):
        if not hasattr(self._tls, "tasks"):
            self._tls.tasks = []
        return self._tls.tasks

    @tasks.setter
    def tasks(self, v):
        self._tls.tasks = v

    @property
    def ai_prompt_parts(self):
        if not hasattr(self._tls, "ai_prompt_parts"):
            self._tls.ai_prompt_parts = []
        return self._tls.ai_prompt_parts

    @ai_prompt_parts.setter
    def ai_prompt_parts(self, v):
        self._tls.ai_prompt_parts = v

    @property
    def in_ai_body(self):
        return getattr(self._tls, "in_ai_body", False)

    @in_ai_body.setter
    def in_ai_body(self, v):
        self._tls.in_ai_body = v

    @property
    def _nursery_stack(self):
        if not hasattr(self._tls, "nursery_stack"):
            self._tls.nursery_stack = []
        return self._tls.nursery_stack

    @property
    def _context_stack(self):
        if not hasattr(self._tls, "context_stack"):
            self._tls.context_stack = []
        return self._tls.context_stack

    # -- setup ------------------------------------------------------
    def emit(self, s):
        self.output.append(s)

    def _io_pool(self):
        """Worker pool for offloading blocking syscalls from tasklets."""
        loop = self.loop
        if loop is None:
            return None
        return getattr(loop, "io_executor", None)

    def err(self, code, message, node=None, hint=""):
        line = getattr(node, "line", 0) if node else 0
        col = getattr(node, "col", 0) if node else 0
        raise NovaError(code, message, line, col, hint)

    def install_builtins(self):
        pass  # builtins are dispatched by name in eval_Call

    def check_fx(self, need, node, what):
        if not need.issubset(self.fx):
            self.err("E_EFFECT",
                     "'%s' is an effect; the current context is pure" % what,
                     node)

    # -- run --------------------------------------------------------
    def run(self, program, filename):
        # Pass 1: register declarations (main thread, before the loop).
        for stmt in program.stmts:
            self.register(stmt)
        if self.is_entry:
            # Root capability grant. Imported modules never get these.
            self.global_env.define("io", VCapability("IO"), False, "cap")
            self.global_env.define("net", VCapability("Net"), False, "cap")
        # v2: the whole program runs as the root tasklet on the
        # cooperative event loop. spawn { } still uses the worker pool
        # for real parallelism; spawn_async { } and async fns are
        # tasklets, serialized by the scheduler.
        loop = EventLoop()
        self.loop = loop
        import concurrent.futures as _cf
        io_exec = _cf.ThreadPoolExecutor(max_workers=4,
                                         thread_name_prefix="nova-io")
        loop.io_executor = io_exec

        def root_body():
            try:
                for stmt in program.stmts:
                    if isinstance(stmt, A.Import):
                        continue
                    self.exec_top(stmt)
                # Structured concurrency: every spawned task completes
                # before the program exits; un-awaited task failures
                # surface here, not silently.
                self._join_tasks(reraise=True)
            finally:
                pass
            return self.output

        try:
            loop.run(root_body)
        except DeadlockError as e:
            raise NovaError("R_DEADLOCK", str(e))
        finally:
            io_exec.shutdown(wait=False)
        # Un-awaited tasklet failures surface here (pool-task failures
        # are already handled by _join_tasks).
        for tl in loop._tasklets:
            if (tl.name != "root" and tl.error is not None
                    and not tl.awaited):
                err = tl.error
                if isinstance(err, TaskletCancelled):
                    continue  # nursery cancellation is expected
                raise (err if isinstance(err, NovaError)
                       else NovaError("R_TASK_FAILED",
                                      "background tasklet failed: %s" % err))
        return self.output

    def register(self, node):
        if isinstance(node, A.Import):
            self.load_module(node)
        elif isinstance(node, (A.FnDecl,)):
            self.global_env.define(node.name, FuncValue(node, self.global_env),
                                   False, "fn")
        elif isinstance(node, A.AiDecl):
            self.global_env.define(node.name,
                                   AiFuncValue(node, self.global_env),
                                   False, "aifn")
        elif isinstance(node, A.ClassDecl):
            self.register_class(node)
        elif isinstance(node, A.InterfaceDecl):
            pass
        elif isinstance(node, A.ComponentDecl):
            self.components[node.name] = node

    def register_class(self, node):
        parent = self.classes.get(node.parent) if node.parent else None
        fields = {}
        if parent:
            fields.update(parent.fields)
        methods = {}
        if parent:
            methods.update(parent.methods)
        init = None
        if node.is_data:
            for m in node.members:  # FieldDecls built by the parser
                fields[m.name] = (None, False)
        else:
            for m in node.members:
                if isinstance(m, A.FieldDecl):
                    fields[m.name] = (None, m.mutable)
                elif isinstance(m, A.InitDecl):
                    init = m
                elif isinstance(m, A.MethodDecl):
                    methods[m.name] = m
        rc = RuntimeClass(node.name, node, parent, fields, methods, init)
        self.classes[node.name] = rc
        self.global_env.define(node.name, rc, False, "class")

    def load_module(self, node):
        if node.name in self.modules:
            return
        from .modpath import resolve
        path = resolve(node.name, self.basedir)
        if path is None:
            self.err("E_IMPORT", "module '%s' not found" % node.name, node)
        real = os.path.realpath(path)
        with self._module_lock:
            if real in self._loaded_modules:
                return
            self._loaded_modules.add(real)
        from .parser import parse_source
        with open(path) as f:
            source = f.read()
        prog = parse_source(source, path)
        sub = Interpreter(os.path.dirname(path), provider=self.provider,
                          is_entry=False, threading=self.threading,
                          executor=self.executor,
                          stream_provider=self.stream_provider)
        sub._loaded_modules = self._loaded_modules
        sub._module_lock = self._module_lock
        sub.run(prog, path)
        attrs = {}
        for name, cell in sub.global_env.cells.items():
            if name.startswith("_") or name in ("io", "net"):
                continue
            attrs[name] = cell.value
        # classes & components travel too
        for name, rc in sub.classes.items():
            if not name.startswith("_"):
                attrs[name] = rc
        for name, cd in sub.components.items():
            if not name.startswith("_"):
                attrs[name] = cd
        local = node.name.split("/")[-1]
        mod = VModule(local, attrs)
        self.modules[local] = mod
        self.global_env.define(local, mod, False, "module")

    def exec_top(self, node):
        if isinstance(node, (A.LetDecl, A.VarDecl)):
            v = self.eval_expr(node.expr, self.global_env)
            self.global_env.define(node.name, v,
                                   isinstance(node, A.VarDecl), "let")
        elif isinstance(node, (A.FnDecl, A.AiDecl, A.ClassDecl,
                               A.InterfaceDecl, A.ComponentDecl)):
            return  # already registered
        elif isinstance(node, A.StreamDecl):
            topic = node.topic or node.name
            self.global_env.define(
                node.name, VStream(topic, self.stream_provider),
                False, "stream")
        elif isinstance(node, A.ContextDecl):
            self._eval_ContextDecl(node, self.global_env)
        else:
            self.exec_stmt(node, self.global_env)

    # -- statements -------------------------------------------------
    def exec_stmt(self, node, env):
        if isinstance(node, A.Block):
            sub = Env(env)
            for s in node.stmts:
                self.exec_stmt(s, sub)
        elif isinstance(node, (A.LetDecl, A.VarDecl)):
            v = self.eval_expr(node.expr, env)
            env.define(node.name, v, isinstance(node, A.VarDecl), "let")
        elif isinstance(node, A.StreamDecl):
            topic = node.topic or node.name
            env.define(node.name, VStream(topic, self.stream_provider),
                       False, "stream")
        elif isinstance(node, A.ContextDecl):
            default = (self.eval_expr(node.default, env)
                       if node.default is not None else VUNIT)
            self.contexts[node.name] = default
        elif isinstance(node, A.ExprStmt):
            self.eval_expr(node.expr, env)
        elif isinstance(node, A.Return):
            v = self.eval_expr(node.expr, env) if node.expr else VUNIT
            raise ReturnExc(v)
        elif isinstance(node, A.IfStmt):
            c = self.eval_expr(node.cond, env)
            if c is True:
                self.exec_stmt(node.then_b, env)
            elif c is False:
                if node.else_b is not None:
                    self.exec_stmt(node.else_b, env)
            else:
                self.err("E_TYPE_MISMATCH", "if condition must be Bool", node)
        elif isinstance(node, A.WhileStmt):
            while True:
                c = self.eval_expr(node.cond, env)
                if c is True:
                    self.exec_stmt(node.body, env)
                elif c is False:
                    break
                else:
                    self.err("E_TYPE_MISMATCH", "while condition must be Bool",
                             node)
        elif isinstance(node, A.ForStmt):
            it = self.eval_expr(node.iter, env)
            if not isinstance(it, VList):
                self.err("E_TYPE_MISMATCH", "for needs a List", node)
            for item in it.items:
                sub = Env(env)
                sub.define(node.var, item, False, "let")
                self.exec_stmt(node.body, sub)
        elif isinstance(node, A.SuperCall):
            self.exec_super_call(node, env)
        else:
            self.err("E_INTERNAL", "unhandled statement", node)

    def exec_super_call(self, node, env):
        this_cell = env.lookup_cell("this")
        inst = this_cell.value
        parent = inst.classdef.parent
        args = [self.eval_expr(a[1], env) for a in node.args]
        self.call_init(parent, inst, args, node)

    # -- expressions ------------------------------------------------
    def eval_expr(self, node, env):
        m = "_eval_" + type(node).__name__
        fn = getattr(self, m, None)
        if fn is None:
            self.err("E_INTERNAL", "no evaluator for %s"
                     % type(node).__name__, node)
        return fn(node, env)

    def _eval_IntLit(self, node, env):
        return node.value

    def _eval_FloatLit(self, node, env):
        return node.value

    def _eval_StrLit(self, node, env):
        return node.value

    def _eval_BoolLit(self, node, env):
        return node.value

    def _eval_NoneLit(self, node, env):
        return VOption(ANY, None)

    def _eval_SomeExpr(self, node, env):
        return VOption(ANY, self.eval_expr(node.expr, env))

    def _eval_OkExpr(self, node, env):
        return VResult(ANY, ANY, True, self.eval_expr(node.expr, env))

    def _eval_ErrExpr(self, node, env):
        return VResult(ANY, ANY, False, self.eval_expr(node.expr, env))

    def _eval_ListLit(self, node, env):
        return VList(ANY, [self.eval_expr(e, env) for e in node.elems])

    def _eval_Var(self, node, env):
        cell = env.lookup_cell(node.name)
        if cell is None:
            self.err("E_UNDEFINED", "undefined name '%s'" % node.name, node)
        v = cell.value
        if v is VUNSET:
            self.err("R_UNSET_FIELD",
                     "field '%s' was never assigned" % node.name, node)
        return v

    def _eval_This(self, node, env):
        cell = env.lookup_cell("this")
        if cell is None:
            self.err("E_THIS", "`this` outside a class", node)
        return cell.value

    def _eval_Super(self, node, env):
        cell = env.lookup_cell("this")
        if cell is None:
            self.err("E_SUPER", "`super` outside a class", node)
        return cell.value  # method calls on super handled via MethodCall

    def _eval_ResultVar(self, node, env):
        cell = env.lookup_cell("result")
        if cell is None:
            self.err("E_RESULT", "`result` outside ensures", node)
        return cell.value

    def _eval_Binary(self, node, env):
        op = node.op
        if op == "and":
            l = self.eval_expr(node.left, env)
            if l is False:
                return False
            return self.eval_expr(node.right, env) is True
        if op == "or":
            l = self.eval_expr(node.left, env)
            if l is True:
                return True
            return self.eval_expr(node.right, env) is True
        l = self.eval_expr(node.left, env)
        r = self.eval_expr(node.right, env)
        if op == "+":
            if isinstance(l, int) and isinstance(r, int) \
                    and not isinstance(l, bool):
                return self._ck(l + r, node, "addition")
            if isinstance(l, float) and isinstance(r, float):
                return l + r
            if isinstance(l, str) and isinstance(r, str):
                return l + r
            self.err("E_TYPE_MISMATCH", "bad operands for +", node)
        if op in ("-", "*", "/", "%"):
            if isinstance(l, int) and isinstance(r, int) \
                    and not isinstance(l, bool) and not isinstance(r, bool):
                return self._int_op(op, l, r, node)
            if isinstance(l, float) and isinstance(r, float):
                if op == "+":
                    return l + r
                if op == "-":
                    return l - r
                if op == "*":
                    return l * r
                if op == "/":
                    return l / r if r != 0 else self.err(
                        "R_DIV_ZERO", "division by zero", node)
                self.err("E_TYPE_MISMATCH", "% on Floats not supported",
                         node)
            self.err("E_TYPE_MISMATCH", "bad operands for %s" % op, node)
        if op in ("==", "!="):
            eq = self._equals(l, r)
            return eq if op == "==" else not eq
        if op in ("<", "<=", ">", ">="):
            if isinstance(l, bool) or isinstance(r, bool) \
                    or type(l) is not type(r) \
                    or not isinstance(l, (int, float)):
                self.err("E_TYPE_MISMATCH",
                         "comparison needs two Ints or two Floats", node)
            return {"<": l < r, "<=": l <= r, ">": l > r, ">=": l >= r}[op]
        self.err("E_INTERNAL", "bad operator", node)

    def _ck(self, v, node, what):
        if v < INT_MIN or v > INT_MAX:
            self.err("R_INT_OVERFLOW",
                     "integer overflow in %s: Nova Ints are signed 64-bit "
                     "and never wrap silently" % what, node,
                     hint="restructure the computation or use Float")
        return v

    def _int_op(self, op, l, r, node):
        if op == "+":
            return self._ck(l + r, node, "addition")
        if op == "-":
            return self._ck(l - r, node, "subtraction")
        if op == "*":
            return self._ck(l * r, node, "multiplication")
        if op == "/":
            if r == 0:
                self.err("R_DIV_ZERO", "integer division by zero", node)
            if l == INT_MIN and r == -1:
                self.err("R_INT_OVERFLOW", "INT_MIN / -1 overflows", node)
            q = abs(l) // abs(r)
            return -q if (l < 0) != (r < 0) else q
        if op == "%":
            if r == 0:
                self.err("R_DIV_ZERO", "modulo by zero", node)
            q = abs(l) // abs(r)
            q = -q if (l < 0) != (r < 0) else q
            return l - q * r

    def _equals(self, l, r):
        if isinstance(l, bool) or isinstance(r, bool):
            return l is r
        if type(l) is not type(r):
            if isinstance(l, VOption) and isinstance(r, VOption):
                if l.value is None or r.value is None:
                    return l.value is None and r.value is None
                return self._equals(l.value, r.value)
            if isinstance(l, VResult) and isinstance(r, VResult):
                return l.is_ok == r.is_ok and self._equals(l.value, r.value)
            return False
        if isinstance(l, VList):
            return len(l.items) == len(r.items) and all(
                self._equals(a, b) for a, b in zip(l.items, r.items))
        if isinstance(l, VInstance):
            if l.classdef.decl.is_data:
                return all(self._equals(l.fields[k], r.fields[k])
                           for k in l.fields)
            return l is r
        return l == r

    def _eval_Unary(self, node, env):
        v = self.eval_expr(node.expr, env)
        if node.op == "not":
            return not v
        if node.op == "-":
            if isinstance(v, bool):
                self.err("E_TYPE_MISMATCH", "unary - on Bool", node)
            if isinstance(v, int):
                return self._ck(-v, node, "negation")
            if isinstance(v, float):
                return -v
            self.err("E_TYPE_MISMATCH", "unary - needs Int or Float", node)

    def _eval_Assign(self, node, env):
        v = self.eval_expr(node.value, env)
        self.assign_target(node.target, v, env)
        return v

    def assign_target(self, target, v, env):
        if isinstance(target, A.Var):
            cell = env.lookup_cell(target.name)
            if cell is None:
                self.err("E_UNDEFINED", "undefined name '%s'" % target.name,
                         target)
            if not cell.mutable:
                self.err("E_IMMUTABLE", "'%s' is immutable" % target.name,
                         target)
            if cell.kind == "state" and not self.fx:
                self.err("E_EFFECT",
                         "mutating state needs an effect context", target)
            cell.value = v
            return
        if isinstance(target, A.Field):
            obj = self.eval_expr(target.obj, env)
            if isinstance(obj, VModule):
                self.err("E_TYPE_MISMATCH",
                         "cannot assign to module attribute", target)
            if isinstance(obj, VInstance):
                if target.name not in obj.fields:
                    self.err("E_NO_FIELD", "no field '%s'" % target.name,
                             target)
                obj.fields[target.name] = v
                return
            self.err("E_TYPE_MISMATCH", "cannot assign to field", target)
        if isinstance(target, A.Index):
            obj = self.eval_expr(target.obj, env)
            idx = self.eval_expr(target.index, env)
            if isinstance(obj, VList) and isinstance(idx, int):
                if not (0 <= idx < len(obj.items)):
                    self.err("R_INDEX_OUT_OF_BOUNDS", "index %d" % idx,
                             target)
                obj.items[idx] = v
                return
            self.err("E_TYPE_MISMATCH", "cannot index-assign", target)
        self.err("E_ASSIGN_TARGET", "cannot assign to this", target)

    def _eval_Question(self, node, env):
        v = self.eval_expr(node.expr, env)
        if isinstance(v, VResult):
            if v.is_ok:
                return v.value
            raise EarlyReturn(VResult(v.ok_t, v.err_t, False, v.value))
        if isinstance(v, VOption):
            if v.value is not None:
                return v.value
            raise EarlyReturn(VOption(v.inner, None))
        self.err("E_QUESTION", "? needs Result or Option", node)

    def _eval_IfExpr(self, node, env):
        c = self.eval_expr(node.cond, env)
        if c is True:
            return self._block_value(node.then_b, env)
        if c is False:
            return self._block_value(node.else_b, env)
        self.err("E_TYPE_MISMATCH", "if condition must be Bool", node)

    def _block_value(self, block, env):
        sub = Env(env)
        v = VUNIT
        for s in block.stmts:
            if isinstance(s, A.ExprStmt):
                v = self.eval_expr(s.expr, sub)
            else:
                self.exec_stmt(s, sub)
        return v

    def _eval_Match(self, node, env):
        v = self.eval_expr(node.scrutinee, env)
        for arm in node.arms:
            bindings = {}
            if self._match_pat(arm.pattern, v, bindings):
                sub = Env(env)
                for k, val in bindings.items():
                    sub.define(k, val, False, "let")
                return self.eval_expr(arm.expr, sub)
        self.err("E_NON_EXHAUSTIVE", "no match arm matched", node)

    def _match_pat(self, pat, v, bindings):
        if isinstance(pat, A.PatWild):
            return True
        if isinstance(pat, A.PatVar):
            bindings[pat.name] = v
            return True
        if isinstance(pat, A.PatSome):
            if isinstance(v, VOption) and v.value is not None:
                bindings[pat.name] = v.value
                return True
            return False
        if isinstance(pat, A.PatNone):
            return isinstance(v, VOption) and v.value is None
        if isinstance(pat, A.PatOk):
            if isinstance(v, VResult) and v.is_ok:
                bindings[pat.name] = v.value
                return True
            return False
        if isinstance(pat, A.PatErr):
            if isinstance(v, VResult) and not v.is_ok:
                bindings[pat.name] = v.value
                return True
            return False
        if isinstance(pat, A.PatLit):
            return type(v) is type(pat.value) and v == pat.value
        return False

    def _eval_Call(self, node, env):
        func = node.func
        args = [(n, self.eval_expr(e, env)) for n, e in node.args]
        if isinstance(func, A.Var):
            return self.call_var(func.name, args, node, env)
        if isinstance(func, A.Field):
            obj = self.eval_expr(func.obj, env)
            if isinstance(obj, VModule):
                if func.name not in obj.attrs:
                    self.err("E_UNDEFINED",
                             "module %s has no '%s'" % (obj.name, func.name),
                             func)
                attr = obj.attrs[func.name]
                return self.call_value(attr, args, node, env,
                                       what="%s.%s" % (obj.name, func.name))
            self.err("E_TYPE_MISMATCH", "cannot call %s" % func.name, func)
        self.err("E_NOT_CALLABLE", "not callable", func)

    def call_var(self, name, args, node, env):
        if name in ("print", "str", "int", "len", "assert", "render",
                    "readFile", "writeFile", "fetch", "prompt", "secret",
                    "sleep", "jsonParse", "toJson", "jsonGet", "jsonStr",
                    "jsonInt", "jsonFloat", "jsonBool", "jsonList", "router",
                    "route", "use", "staticDir", "serve", "response",
                    "jsonResponse", "reqMethod", "reqPath", "reqBody",
                    "reqJson", "param", "paramInt", "query", "header", "env",
                    "parseInt", "parseFloat", "similarity", "topK",
                    "dbOpen", "dbTable", "dbExec",
                    "get", "post", "put", "delete", "patch"):
            return self.call_builtin(name, args, node, env)
        if name in self.components:
            return self.instantiate(self.components[name], args, node)
        cell = env.lookup_cell(name)
        if cell is None:
            self.err("E_UNDEFINED", "undefined name '%s'" % name, node)
        return self.call_value(cell.value, args, node, env, what=name)

    def call_value(self, fv, args, node, env, what):
        if isinstance(fv, FuncValue):
            return self.call_fn(fv, args, node, what)
        if isinstance(fv, AiFuncValue):
            return self.call_ai(fv, args, node, what)
        if isinstance(fv, RuntimeClass):
            return self.construct(fv, args, node)
        if isinstance(fv, A.ComponentDecl):
            return self.instantiate(fv, args, node)
        self.err("E_NOT_CALLABLE", "'%s' is not callable" % what, node)

    def bind_args(self, params, args, node, what):
        # params: [Param]; args: [(name|None, value)]
        bound = {}
        pos = 0
        for aname, aval in args:
            if aname is not None:
                pname = next((p.name for p in params if p.name == aname),
                             None)
                if pname is None:
                    self.err("E_ARGC", "%s has no parameter '%s'"
                             % (what, aname), node)
            else:
                if pos >= len(params):
                    self.err("E_ARGC", "%s takes %d argument(s)"
                             % (what, len(params)), node)
                pname = params[pos].name
                pos += 1
            if pname in bound:
                self.err("E_ARGC", "duplicate argument '%s'" % pname, node)
            bound[pname] = aval
        missing = [p.name for p in params if p.name not in bound]
        if missing:
            self.err("E_ARGC", "%s missing arguments: %s"
                     % (what, ", ".join(missing)), node)
        return bound

    def call_fn(self, fv, args, node, what):
        decl = fv.decl
        need = EFFECT_OF[decl.effect if isinstance(decl, A.FnDecl)
                         else decl.effect]
        # "async" marks await-capability, not a caller barrier (mirrors the
        # checker's _check_effect).
        if not (need - {"async"}).issubset(self.fx):
            self.err("E_EFFECT", "calling '%s' needs effect %s"
                     % (what, sorted(need)), node)
        bound = self.bind_args(decl.params, args, node, what)
        call_env = Env(fv.closure)
        if fv.bound_this is not None:
            call_env.define("this", fv.bound_this, False, "this")
        for pname, pval in bound.items():
            call_env.define(pname, pval, False, "param")
        saved_fx = self.fx
        self.fx = need
        try:
            for req in decl.requires:
                if self.eval_expr(req, call_env) is not True:
                    self.err("R_CONTRACT_PRE",
                             "precondition violated in '%s'" % what, node,
                             hint="requires clause failed")
            try:
                self.exec_stmt(decl.body, call_env)
                result = VUNIT
            except ReturnExc as r:
                result = r.value
            except EarlyReturn as e:
                result = e.value
            call_env.define("result", result, False, "result")
            for ens in decl.ensures:
                if self.eval_expr(ens, call_env) is not True:
                    self.err("R_CONTRACT_POST",
                             "postcondition violated in '%s'" % what, node,
                             hint="ensures clause failed")
            return result
        finally:
            self.fx = saved_fx

    def construct(self, rc, args, node):
        if rc.decl.is_data:
            params = [A.Param(name=n, type=None)
                      for n in rc.fields.keys()]
            bound = self.bind_args(params, args, node,
                                   what=rc.name)
            return VInstance(rc, dict(bound))
        bound = {}
        inst = VInstance(rc, {n: VUNSET for n in rc.fields})
        if rc.init is not None:
            bound = self.bind_args(rc.init.params, args, node,
                                   what="init of " + rc.name)
            init_env = Env(self.global_env)
            init_env.define("this", inst, False, "this")
            for pname, pval in bound.items():
                init_env.define(pname, pval, False, "param")
            saved_fx = self.fx
            self.fx = CONSOLE
            try:
                self.exec_stmt(rc.init.body, init_env)
            finally:
                self.fx = saved_fx
        elif args:
            self.err("E_ARGC", "%s takes no arguments" % rc.name, node)
        return inst

    def call_init(self, rc, inst, args, node):
        bound = self.bind_args(rc.init.params,
                               [(None, a) for a in args], node,
                               what="super()")
        init_env = Env(self.global_env)
        init_env.define("this", inst, False, "this")
        for pname, pval in bound.items():
            init_env.define(pname, pval, False, "param")
        saved_fx = self.fx
        self.fx = CONSOLE
        try:
            self.exec_stmt(rc.init.body, init_env)
        finally:
            self.fx = saved_fx

    def _eval_query_method(self, q, node, args):
        from .db import run_query, DbError
        name = node.name
        if name == "select":
            cols = args[0][1]
            if not isinstance(cols, VList):
                self.err("E_TYPE_MISMATCH", "select needs List[String]",
                         node)
            nq = q.clone()
            nq._select = [c for c in cols.items]
            return nq
        if name == "where":
            col, op, val = args[0][1], args[1][1], args[2][1]
            nq = q.clone()
            nq._wheres.append((col, op, _query_scalar(val, node, self)))
            return nq
        if name == "orderBy":
            nq = q.clone()
            nq._order = (args[0][1], args[1][1] if len(args) > 1 else "asc")
            return nq
        if name == "limit":
            nq = q.clone()
            nq._limit = args[0][1]
            return nq
        if name == "all":
            self.check_fx(IO_EFF, node, "query.all")
            try:
                rows = run_query(q)
            except DbError as e:
                return VResult(ListType(JSON), STRING, False, str(e))
            return VResult(ListType(JSON), STRING, True,
                           VList(JSON, [VJson(r) for r in rows]))
        if name == "first":
            self.check_fx(IO_EFF, node, "query.first")
            try:
                rows = run_query(q)
            except DbError as e:
                return VResult(OptionType(JSON), STRING, False, str(e))
            v = VJson(rows[0]) if rows else None
            return VResult(OptionType(JSON), STRING, True,
                           VOption(JSON, v))
        self.err("E_NO_METHOD", "Query has no method '%s'" % name, node)

    def _eval_MethodCall(self, node, env):
        obj = self.eval_expr(node.obj, env)
        args = [(n, self.eval_expr(e, env)) for n, e in node.args]
        if isinstance(obj, VList) and node.name == "push":
            if len(args) != 1:
                self.err("E_ARGC", "push takes 1 argument", node)
            obj.items.append(args[0][1])
            return VUNIT
        if isinstance(obj, VOption) and node.name in ("unwrap", "expect"):
            if obj.value is None:
                msg = args[0][1] if args else "unwrapped None"
                self.err("R_UNWRAP_NONE", "unwrap on None: %s" % msg, node)
            return obj.value
        if isinstance(obj, VResult) and node.name in ("unwrap", "expect"):
            if not obj.is_ok:
                msg = args[0][1] if args else "unwrapped Err"
                self.err("R_UNWRAP_ERR", "unwrap on Err: %s" % msg, node)
            return obj.value
        if isinstance(obj, VQuery):
            return self._eval_query_method(obj, node, args)
        if isinstance(obj, VCompInstance) and node.name == "fire":
            if len(args) != 1 or not isinstance(args[0][1], str):
                self.err("E_ARGC", "fire takes an event name string", node)
            self.check_fx(CONSOLE, node, "fire")
            self.fire_event(obj, args[0][1], node)
            return VUNIT
        if isinstance(obj, VTask) and node.name == "await":
            self.check_fx(CONSOLE, node, "await")
            if len(args):
                self.err("E_ARGC", "await takes no arguments", node)
            return self.await_task(obj, node)
        if isinstance(obj, VStream) and node.name == "publish":
            self.check_fx(CONSOLE, node, "publish")
            if len(args) == 1 and args[0][0] is None:
                obj.publish(None, args[0][1])
            elif (len(args) == 2 and args[0][0] == "key"
                    and args[1][0] == "value"):
                if not isinstance(args[0][1], str):
                    self.err("E_TYPE_MISMATCH", "key must be a String",
                             node)
                obj.publish(args[0][1], args[1][1])
            else:
                self.err("E_ARGC",
                         "publish(value) or publish(key: String, value)",
                         node)
            return VUNIT
        if isinstance(obj, VStream) and node.name == "subscribe":
            self.check_fx(CONSOLE, node, "subscribe")
            if len(args) != 1 or args[0][0] not in (None, "group"):
                self.err("E_ARGC", "subscribe takes group: String", node)
            group = args[0][1]
            if not isinstance(group, str):
                self.err("E_TYPE_MISMATCH", "group must be a String", node)
            return VSubscription(obj, group)
        if isinstance(obj, VSubscription) and node.name == "poll":
            self.check_fx(CONSOLE, node, "poll")
            if len(args):
                self.err("E_ARGC", "poll takes no arguments", node)
            rec = obj.stream.provider.poll(obj.stream.topic, obj.group)
            if rec is None:
                return VOption(None, None)
            offset, key, value = rec
            return VOption(None, VMessage(obj.stream.topic, offset, key,
                                          value))
        if isinstance(obj, VSubscription) and node.name == "commit":
            self.check_fx(CONSOLE, node, "commit")
            if len(args) != 1 or args[0][0] is not None:
                self.err("E_ARGC", "commit takes the consumed message",
                         node)
            msg = args[0][1]
            if not isinstance(msg, VMessage):
                self.err("E_TYPE_MISMATCH", "commit needs a Message", node)
            obj.stream.provider.commit(obj.stream.topic, obj.group,
                                       msg.offset)
            return VUNIT
        if isinstance(obj, VModule):
            if node.name not in obj.attrs:
                self.err("E_UNDEFINED", "module %s has no '%s'"
                         % (obj.name, node.name), node)
            v = obj.attrs[node.name]
            if isinstance(v, FuncValue):
                return self.call_fn(v, args, node, node.name)
            if isinstance(v, RuntimeClass):
                return self.construct(v, args, node)
            if isinstance(v, A.ComponentDecl):
                return self.instantiate(v, args, node)
            self.err("E_NOT_CALLABLE", "'%s' is not callable" % node.name,
                     node)
        if isinstance(obj, VInstance):
            m = obj.classdef.methods.get(node.name)
            if m is None:
                self.err("E_NO_METHOD", "no method '%s'" % node.name, node)
            return self.call_fn(FuncValue(m, self.global_env, bound_this=obj),
                                args, node, node.name)
        self.err("E_NO_METHOD", "no method '%s'" % node.name, node)

    def _eval_Field(self, node, env):
        obj = self.eval_expr(node.obj, env)
        if isinstance(obj, VMessage):
            if node.name == "topic":
                return obj.topic
            if node.name == "offset":
                return obj.offset
            if node.name == "key":
                return VOption(None, obj.key)
            if node.name == "value":
                return obj.value
            self.err("E_NO_FIELD", "Message has no field '%s'" % node.name,
                     node)
        if isinstance(obj, VInstance):
            if node.name not in obj.fields:
                self.err("E_NO_FIELD", "no field '%s'" % node.name, node)
            v = obj.fields[node.name]
            if v is VUNSET:
                self.err("R_UNSET_FIELD",
                         "field '%s' was never assigned" % node.name, node)
            return v
        if isinstance(obj, VModule):
            if node.name not in obj.attrs:
                self.err("E_UNDEFINED", "module %s has no '%s'"
                         % (obj.name, node.name), node)
            return obj.attrs[node.name]
        self.err("E_NO_FIELD", "no field '%s'" % node.name, node)

    def _eval_Index(self, node, env):
        obj = self.eval_expr(node.obj, env)
        idx = self.eval_expr(node.index, env)
        if isinstance(obj, VList) and isinstance(idx, int) \
                and not isinstance(idx, bool):
            if not (0 <= idx < len(obj.items)):
                self.err("R_INDEX_OUT_OF_BOUNDS", "index %d" % idx, node)
            return obj.items[idx]
        self.err("E_TYPE_MISMATCH", "cannot index", node)

    # -- components -------------------------------------------------
    def instantiate(self, compdef, args, node):
        bound = self.bind_args(compdef.params, args, node,
                               what=compdef.name)
        state = {}
        for s in compdef.states:
            v = self.eval_expr(s.init, self.global_env)
            state[s.name] = Cell(v, True, "state")
        return VCompInstance(compdef, bound, state)

    def render_tree(self, inst):
        env = Env(self.global_env, state_cells=inst.state)
        for pname, pval in inst.props.items():
            env.define(pname, pval, False, "prop")
        saved_fx = self.fx
        self.fx = PURE
        try:
            children = []
            for c in inst.compdef.view:
                children.extend(self.eval_jsx(c, env))
        finally:
            self.fx = saved_fx
        # wrap in a fragment root for uniform handling
        return VNode("__root__", [], {}, children)

    def eval_jsx(self, node, env):
        if isinstance(node, A.JText):
            return [node.text]
        if isinstance(node, A.JExpr):
            v = self.eval_expr(node.expr, env)
            return [nova_repr(v)]
        if isinstance(node, A.JFor):
            it = self.eval_expr(node.iter, env)
            if not isinstance(it, VList):
                self.err("E_TYPE_MISMATCH", "<for> needs a List", node)
            out = []
            for item in it.items:
                sub = Env(env)
                sub.define(node.var, item, False, "let")
                for c in node.children:
                    out.extend(self.eval_jsx(c, sub))
            return out
        if isinstance(node, A.JElement):
            if node.is_component:
                compdef = self.components.get(node.tag)
                if compdef is None:
                    self.err("E_UNDEFINED", "unknown component", node)
                args = []
                for attr in node.attrs:
                    if _is_event(attr.name):
                        self.err("E_TYPE_MISMATCH",
                                 "event handlers on components not supported "
                                 "in v1", attr)
                    v = (attr.value.value if isinstance(attr.value, A.StrLit)
                         else self.eval_expr(attr.value, env))
                    args.append((attr.name, v))
                child = self.instantiate(compdef, args, node)
                subtree = self.render_tree(child)
                return subtree.children
            attrs = []
            handlers = {}
            for attr in node.attrs:
                if _is_event(attr.name):
                    handlers[attr.name] = attr.value
                elif attr.value is None:
                    attrs.append((attr.name, None))
                elif isinstance(attr.value, A.StrLit):
                    attrs.append((attr.name, attr.value.value))
                else:
                    attrs.append((attr.name,
                                  nova_repr(self.eval_expr(attr.value, env))))
            children = []
            for c in node.children:
                children.extend(self.eval_jsx(c, env))
            return [VNode(node.tag, attrs, handlers, children)]
        self.err("E_INTERNAL", "bad JSX node", node)

    def fire_event(self, inst, event_name, node):
        target = self._find_handler(inst.tree, event_name)
        if target is None:
            self.err("R_NO_HANDLER", "no '%s' handler rendered" % event_name,
                     node)
        env = Env(self.global_env, state_cells=inst.state)
        for pname, pval in inst.props.items():
            env.define(pname, pval, False, "prop")
        saved_fx = self.fx
        self.fx = TOP_EFF
        try:
            self.eval_expr(target, env)
        finally:
            self.fx = saved_fx
        self.do_render(inst)

    def _find_handler(self, vnode, event_name):
        if event_name in vnode.handlers:
            return vnode.handlers[event_name]
        for c in vnode.children:
            if isinstance(c, VNode):
                found = self._find_handler(c, event_name)
                if found is not None:
                    return found
        return None

    # -- concurrency --------------------------------------------------
    # Default model: single-threaded event loop. spawn { } opts into real
    # parallelism on a worker pool (or inline execution under threading =
    # "single"). Tasks are structured: a task's children complete before it
    # does, and every outstanding task is joined before the program exits.
    def _register_task(self, t):
        # Tasks spawned inside a nursery belong to the nursery; otherwise
        # they are joined before the current tasklet (or program) exits.
        if self._nursery_stack:
            self._nursery_stack[-1].append(t)
        else:
            self.tasks.append(t)

    def _eval_Spawn(self, node, env):
        self.check_fx(CONSOLE, node, "spawn")
        if self.threading == "single" or self.executor is None:
            # single-threaded model: run inline, deterministically
            val = self._eval_block_value(node.block, Env(env))
            import concurrent.futures as _cf
            f = _cf.Future()
            f.set_result(val)
            t = VTask(future=f, interp=None)
            t.merged = True
            t.awaited = True
            return t
        task_interp = self.clone_for_task()
        fut = self.executor.submit(task_interp._run_task_block,
                                   node.block, env)
        t = VTask(future=fut, interp=task_interp)
        self._register_task(t)
        return t

    def _eval_SpawnAsync(self, node, env):
        # spawn_async { block } — cooperative tasklet on the event loop.
        # Serialized by the scheduler: never parallel, no data races
        # with other tasklets. Shares this interpreter (and its output).
        self.check_fx(CONSOLE, node, "spawn_async")
        t = current_tasklet()
        if t is None or self.loop is None:
            self.err("R_TASKLET_CONTEXT",
                     "spawn_async { } needs the event loop", node)
        inherited_ctx = list(self._context_stack)

        def body():
            # Lexical context flow: the tasklet sees the spawner's
            # provides. Its own outstanding tasks join before it ends.
            self._tls.context_stack = inherited_ctx
            try:
                return self._eval_block_value(node.block, Env(env))
            finally:
                self._join_tasks()

        tasklet = self.loop.spawn_tasklet(body, "spawn-async")
        vt = VTask(tasklet=tasklet)
        self._register_task(vt)
        return vt

    def _eval_Nursery(self, node, env):
        # Structured scope: every task spawned inside is joined before
        # the block exits — on ALL exits. On error, tasks are cancelled
        # (best-effort) and joined suppressively; the original error wins.
        frame = []
        self._nursery_stack.append(frame)
        try:
            val = self._eval_block_value(node.block, Env(env))
        except BaseException:
            self._nursery_cancel(frame)
            self._nursery_join(frame, suppress=True)
            self._nursery_stack.pop()
            raise
        self._nursery_join(frame, suppress=False)
        self._nursery_stack.pop()
        return val

    def _nursery_cancel(self, frame):
        for t in frame:
            if t.tasklet is not None:
                t.tasklet.request_cancel()
            elif t.future is not None:
                try:
                    t.future.cancel()
                except Exception:
                    pass

    def _nursery_join(self, frame, suppress):
        t = current_tasklet()
        for task in frame:
            task.awaited = True
            try:
                if task.tasklet is not None:
                    tl = task.tasklet
                    if t is not None:
                        t.park(TaskletAwaitable(tl))
                    else:
                        tl.wait_blocking()
                elif task.future is not None:
                    if t is not None:
                        t.park(TaskAwaitable(task.future))
                    else:
                        task.future.result()
            except TaskletCancelled as e:
                if not suppress:
                    raise NovaError("R_CANCELLED", str(e))
            except NovaError:
                if not suppress:
                    raise
            except Exception as e:
                if not suppress:
                    self.err("R_TASK_FAILED",
                             "nursery task failed: %s" % e)
            if (task.interp is not None and task.tasklet is None
                    and not task.merged):
                self.output.extend(task.interp.output)
                task.merged = True

    def _eval_ContextDecl(self, node, env):
        # Evaluated once at declaration time; the value is the default
        # seen by `consume` when no provider is active.
        default = (self.eval_expr(node.default, env)
                   if node.default is not None else VUNIT)
        self.contexts[node.name] = default
        return VUNIT

    def _eval_Provide(self, node, env):
        if node.name not in self.contexts:
            self.err("E_UNDEFINED", "unknown context '%s'" % node.name,
                     node)
        val = self.eval_expr(node.value, env)
        self._context_stack.append({node.name: val})
        try:
            return self._eval_block_value(node.block, Env(env))
        finally:
            self._context_stack.pop()

    def _eval_Consume(self, node, env):
        for frame in reversed(self._context_stack):
            if node.name in frame:
                return frame[node.name]
        if node.name in self.contexts:
            return self.contexts[node.name]
        self.err("E_UNDEFINED", "unknown context '%s'" % node.name, node)

    def _eval_block_value(self, block, env):
        sub = Env(env)
        v = VUNIT
        for s in block.stmts:
            if isinstance(s, A.ExprStmt):
                v = self.eval_expr(s.expr, sub)
            else:
                self.exec_stmt(s, sub)
        return v

    def clone_for_task(self):
        # A task shares code (classes/components/modules), the worker pool
        # and the stream provider — but gets a FRESH effect context, output
        # buffer and AI state. Name resolution captures the spawn site's
        # environment lexically (share immutable data; v2 will check Send).
        sub = Interpreter(self.basedir, provider=self.provider,
                          is_entry=False, threading=self.threading,
                          executor=self.executor,
                          stream_provider=self.stream_provider)
        sub._loaded_modules = self._loaded_modules
        sub._module_lock = self._module_lock
        sub.classes = self.classes
        sub.components = self.components
        sub.modules = self.modules
        sub._fx_init = TOP_EFF  # init value: pool threads pick this up
        return sub

    def _run_task_block(self, block, site_env):
        try:
            return self._eval_block_value(block, Env(site_env))
        except EarlyReturn:
            self.err("R_TASK_LEAK", "? cannot escape a spawn block", block)
        except ReturnExc:
            self.err("R_TASK_LEAK",
                     "return cannot escape a spawn block — the block's last "
                     "expression is the task's value", block)
        finally:
            # structured: children finish before the task completes
            self._join_tasks()

    def await_task(self, task, node):
        # Awaiting is a yield point: on the event loop the tasklet parks
        # (genuine suspend/resume); on a worker thread it blocks.
        task.awaited = True
        t = current_tasklet()
        try:
            if task.tasklet is not None:
                tl = task.tasklet
                tl.awaited = True
                if tl is t:
                    self.err("R_DEADLOCK",
                             "a tasklet cannot await itself", node)
                if t is not None:
                    val = t.park(TaskletAwaitable(tl))
                else:
                    val = tl.wait_blocking()
            elif t is not None:
                val = t.park(TaskAwaitable(task.future))
            else:
                val = task.future.result()
        except TaskletCancelled as e:
            self.err("R_CANCELLED", str(e), node)
        except NovaError:
            raise
        except Exception as e:
            self.err("R_TASK_FAILED", "task failed: %s" % e, node)
        if not task.merged and task.tasklet is None:
            if task.interp is not None:
                self.output.extend(task.interp.output)
            task.merged = True
        return val

    def _join_tasks(self, reraise=False):
        t = current_tasklet()
        for task in self.tasks:
            if task.merged:
                continue
            try:
                if task.tasklet is not None:
                    tl = task.tasklet
                    task.awaited = True
                    tl.awaited = True
                    if t is not None:
                        t.park(TaskletAwaitable(tl))
                    else:
                        tl.wait_blocking()
                elif t is not None:
                    t.park(TaskAwaitable(task.future))
                else:
                    task.future.result()
            except TaskletCancelled:
                if reraise and not task.awaited:
                    raise NovaError("R_CANCELLED", "task was cancelled")
            except NovaError:
                if reraise and not task.awaited:
                    raise
                # otherwise the failure surfaces at the await point
            except Exception as e:
                if reraise and not task.awaited:
                    self.err("R_TASK_FAILED", "task failed: %s" % e)
            if task.interp is not None and task.tasklet is None:
                self.output.extend(task.interp.output)
            task.merged = True

    def do_render(self, inst):
        inst.tree = self.render_tree(inst)
        for child in inst.tree.children:
            for line in format_tree(child, 0):
                self.emit(line)
        self.emit("--- html ---")
        self.emit("".join(format_html(c) for c in inst.tree.children))

    # -- ai ---------------------------------------------------------
    def call_ai(self, fv, args, node, what):
        decl = fv.decl
        if not NET_EFF.issubset(self.fx):
            self.err("E_EFFECT", "calling ai fn '%s' needs effect" % what,
                     node)
        bound = self.bind_args(decl.params, args, node, what)
        call_env = Env(fv.closure)
        for pname, pval in bound.items():
            call_env.define(pname, pval, False, "param")
        saved_fx = self.fx
        saved_prompt = self.ai_prompt_parts
        saved_in_ai = self.in_ai_body
        self.fx = NET_EFF
        self.ai_prompt_parts = []
        self.in_ai_body = True
        try:
            self.exec_stmt(decl.body, call_env)
            prompt = "\n".join(self.ai_prompt_parts)
            if not prompt.strip():
                self.err("R_AI_NO_PROMPT",
                         "ai fn '%s' must call prompt(...)" % what, node)
            return self.provider_complete(decl, prompt, node)
        finally:
            self.fx = saved_fx
            self.ai_prompt_parts = saved_prompt
            self.in_ai_body = saved_in_ai

    def provider_complete(self, decl, prompt, node):
        from .providers import AIRequest, select_provider
        provider = select_provider(self.provider)
        schema = self._resolve_schema(decl.ret, node)
        req = AIRequest(
            model=decl.config.get("model", "nova-mock-1"),
            prompt=prompt,
            schema=schema,
            schema_json=self.schema_json(schema),
            temperature=decl.config.get("temperature", 0.7),
            max_tokens=decl.config.get("max_tokens", 512),
            stream=bool(decl.config.get("stream", False)),
            build_mock=lambda t, p: self.build_mock(t, p),
            coerce_json=lambda data, t: self.coerce_json(data, t, node),
        )
        try:
            t = current_tasklet()
            if t is not None and self._io_pool() is not None:
                # On the event loop: the provider may do network IO —
                # offload and park instead of stalling the scheduler.
                return t.park(TaskAwaitable(
                    self._io_pool().submit(provider.complete, req)))
            return provider.complete(req)
        except NovaError:
            raise
        except Exception as e:
            self.err("R_AI_PROVIDER", "AI provider failed: %s" % e, node)

    def _ai_checker(self):
        c = Checker(self.basedir, "<ai>", is_entry=False)
        for name, rc in self.classes.items():
            c.classes[name] = ClassInfo(
                name, rc.parent.name if rc.parent else None, [],
                rc.decl.is_data)
            c.class_nodes[name] = rc.decl
        return c

    def _resolve_schema(self, tnode, node):
        # Resolve the ai fn's return TypeNode against runtime classes.
        return self._ai_checker().resolve_type(tnode)

    def schema_json(self, t):
        from .types import ListType, OptionType, ClassType, VectorType
        if isinstance(t, VectorType):
            return {"type": "array", "items": {"type": "number"},
                    "minItems": t.dim, "maxItems": t.dim}
        if t == INT:
            return {"type": "integer"}
        if t == FLOAT:
            return {"type": "number"}
        if t == STRING:
            return {"type": "string"}
        if t == BOOL:
            return {"type": "boolean"}
        if isinstance(t, ListType):
            return {"type": "array", "items": self.schema_json(t.elem)}
        if isinstance(t, OptionType):
            return {"anyOf": [self.schema_json(t.elem), {"type": "null"}]}
        if isinstance(t, ClassType):
            rc = self.classes[t.name]
            props = {}
            for fname in rc.fields:
                # field types unknown at runtime; default to string
                props[fname] = {"type": "string"}
            return {"type": "object", "title": t.name, "properties": props,
                    "required": list(props)}
        return {}

    def build_mock(self, t, prompt, depth=0):
        from .types import (ListType, OptionType, ResultType, ClassType,
                            VectorType)
        if isinstance(t, VectorType):
            return VVector(t.dim, _mock_embedding(prompt, t.dim))
        if t == INT:
            return 42
        if t == FLOAT:
            return 0.5
        if t == STRING:
            return "[mock] " + prompt[:48]
        if t == BOOL:
            return True
        if isinstance(t, ListType):
            if depth >= 3:
                return VList(t.elem, [])
            return VList(t.elem, [self.build_mock(t.elem, prompt, depth + 1)])
        if isinstance(t, OptionType):
            if depth >= 3:
                return VOption(t.elem, None)
            return VOption(t.elem,
                           self.build_mock(t.elem, prompt, depth + 1))
        if isinstance(t, ResultType):
            return VResult(t.ok, t.err, True,
                           self.build_mock(t.ok, prompt, depth + 1))
        if isinstance(t, ClassType):
            if depth >= 3:
                return VUNIT
            rc = self.classes[t.name]
            c = self._ai_checker()
            fields = {}
            for m in rc.decl.members:
                if isinstance(m, A.FieldDecl):
                    fields[m.name] = self.build_mock(
                        c.resolve_type(m.type), prompt, depth + 1)
            return VInstance(rc, fields)
        return VUNIT

    def coerce_json(self, data, t, node):
        from .types import ListType, OptionType, ResultType, ClassType
        if t == INT:
            return int(data)
        if t == FLOAT:
            return float(data)
        if t == STRING:
            return str(data)
        if t == BOOL:
            return bool(data)
        if isinstance(t, ListType):
            return VList(t.elem,
                         [self.coerce_json(x, t.elem, node) for x in data])
        if isinstance(t, OptionType):
            if data is None:
                return VOption(t.elem, None)
            return VOption(t.elem, self.coerce_json(data, t.elem, node))
        if isinstance(t, ClassType):
            rc = self.classes[t.name]
            fields = {f: self.coerce_json(data.get(f, ""), STRING, node)
                      for f in rc.fields}
            return VInstance(rc, fields)
        return VUNIT

    # -- builtins ---------------------------------------------------
    def call_builtin(self, name, args, node, env):
        vals = [v for _, v in args]
        if name == "print":
            self.check_fx(CONSOLE, node, "print")
            self.emit(" ".join(nova_repr(v) for v in vals))
            return VUNIT
        if name == "str":
            return nova_repr(vals[0])
        if name == "int":
            import math
            t = math.trunc(vals[0])
            if t < INT_MIN or t > INT_MAX:
                self.err("R_INT_OVERFLOW", "int() out of range", node)
            return t
        if name == "len":
            return len(vals[0].items)
        if name == "assert":
            if vals[0] is not True:
                msg = vals[1] if len(vals) > 1 else "assertion failed"
                self.err("R_ASSERT", "assert failed: %s" % msg, node)
            return VUNIT
        if name == "render":
            self.check_fx(CONSOLE, node, "render")
            inst = vals[0]
            if not isinstance(inst, VCompInstance):
                self.err("E_TYPE_MISMATCH", "render needs a component", node)
            self.do_render(inst)
            return inst
        if name == "readFile":
            self.check_fx(IO_EFF, node, "readFile")
            cap = vals[1]
            if not isinstance(cap, VCapability) or cap.name != "IO":
                self.err("E_CAPABILITY", "readFile needs an IO capability",
                         node)
            path = vals[0]
            t = current_tasklet()
            if t is not None and self._io_pool() is not None:
                # On the event loop: offload the blocking syscall and
                # park the tasklet — genuine async IO, no stalled loop.
                return t.park(TaskAwaitable(
                    self._io_pool().submit(_do_readFile, path)))
            return _do_readFile(path)
        if name == "writeFile":
            self.check_fx(IO_EFF, node, "writeFile")
            cap = vals[2]
            if not isinstance(cap, VCapability) or cap.name != "IO":
                self.err("E_CAPABILITY", "writeFile needs an IO capability",
                         node)
            path, content = vals[0], vals[1]
            t = current_tasklet()
            if t is not None and self._io_pool() is not None:
                return t.park(TaskAwaitable(
                    self._io_pool().submit(_do_writeFile, path, content)))
            return _do_writeFile(path, content)
        if name == "fetch":
            self.check_fx(NET_EFF, node, "fetch")
            cap = vals[1]
            if not isinstance(cap, VCapability) or cap.name != "Net":
                self.err("E_CAPABILITY", "fetch needs a Net capability",
                         node)
            url = vals[0]
            t = current_tasklet()
            if t is not None and self._io_pool() is not None:
                # EXPERIMENTAL: minimal HTTPS GET via stdlib, offloaded.
                return t.park(TaskAwaitable(
                    self._io_pool().submit(_do_fetch, url)))
            return _do_fetch(url)
        if name == "sleep":
            self.check_fx(CONSOLE, node, "sleep")
            secs = float(vals[0])
            t = current_tasklet()
            if t is not None:
                # Cooperative park on the loop's timer — other tasklets
                # run while this one sleeps. Genuine suspend/resume.
                t.park(SleepAwaitable(secs))
            else:
                # Worker thread (spawn { }): just block the thread.
                time.sleep(secs)
            return VUNIT
        if name == "prompt":
            if not self.in_ai_body:
                self.err("E_AI_PROMPT",
                         "prompt() only valid inside an ai fn body", node)
            self.ai_prompt_parts.append(vals[0])
            return VUNIT
        if name == "secret":
            self.check_fx(IO_EFF, node, "secret")
            cap = vals[1]
            if not isinstance(cap, VCapability) or cap.name != "IO":
                self.err("E_CAPABILITY", "secret needs an IO capability",
                         node)
            # v1 secret store: environment variables. The secret is
            # referenced BY NAME and never appears in source.
            env_name = "NOVA_SECRET_" + vals[0].upper().replace("-", "_")
            val = os.environ.get(env_name)
            if val is None:
                return VResult(STRING, STRING, False,
                               "secret not found: " + vals[0])
            return VResult(STRING, STRING, True, val)
        # -- JSON --------------------------------------------------
        if name == "jsonParse":
            import json as _json
            try:
                return VResult(JSON, STRING, True,
                               VJson(_json.loads(vals[0])))
            except Exception as e:
                return VResult(JSON, STRING, False, "invalid JSON: %s" % e)
        if name == "toJson":
            try:
                return json_dumps(vals[0])
            except TypeError as e:
                self.err("E_TYPE_MISMATCH", str(e), node)
        if name == "jsonGet":
            j, key = vals[0], vals[1]
            if not isinstance(j, VJson) or not isinstance(j.value, dict):
                return VOption(JSON, None)
            v = j.value.get(key)
            return VOption(JSON, VJson(v) if v is not None else None)
        if name == "jsonStr":
            j = vals[0]
            v = j.value if isinstance(j, VJson) else None
            return VOption(STRING, v if isinstance(v, str) else None)
        if name == "jsonInt":
            j = vals[0]
            v = j.value if isinstance(j, VJson) else None
            if isinstance(v, bool):
                return VOption(INT, None)
            if isinstance(v, int):
                out = v
            elif isinstance(v, float) and v.is_integer():
                out = int(v)
            else:
                return VOption(INT, None)
            if out < INT_MIN or out > INT_MAX:
                return VOption(INT, None)
            return VOption(INT, out)
        if name == "jsonFloat":
            j = vals[0]
            v = j.value if isinstance(j, VJson) else None
            if isinstance(v, bool):
                return VOption(FLOAT, None)
            if isinstance(v, (int, float)):
                return VOption(FLOAT, float(v))
            return VOption(FLOAT, None)
        if name == "jsonBool":
            j = vals[0]
            v = j.value if isinstance(j, VJson) else None
            return VOption(BOOL, v if isinstance(v, bool) else None)
        if name == "jsonList":
            j = vals[0]
            v = j.value if isinstance(j, VJson) else None
            if not isinstance(v, list):
                return VOption(ListType(JSON), None)
            return VOption(ListType(JSON),
                           VList(JSON, [VJson(x) for x in v]))
        # -- misc --------------------------------------------------
        if name == "env":
            v = os.environ.get(vals[0])
            return VOption(STRING, v)
        if name == "parseInt":
            try:
                v = int(vals[0].strip(), 10)
            except (ValueError, AttributeError):
                return VOption(INT, None)
            if v < INT_MIN or v > INT_MAX:
                return VOption(INT, None)
            return VOption(INT, v)
        if name == "parseFloat":
            try:
                return VOption(FLOAT, float(vals[0].strip()))
            except (ValueError, AttributeError):
                return VOption(FLOAT, None)
        # -- vectors (RAG-native) ----------------------------------
        if name == "similarity":
            a, b = vals[0], vals[1]
            if not isinstance(a, VVector) or not isinstance(b, VVector):
                self.err("E_TYPE_MISMATCH", "similarity needs Vectors",
                         node)
            if a.dim != b.dim:
                self.err("R_VECTOR_DIM",
                         "vector dimensions differ: %d vs %d"
                         % (a.dim, b.dim), node)
            return _cosine(a.items, b.items)
        if name == "topK":
            q, vecs, k = vals[0], vals[1], vals[2]
            if not isinstance(q, VVector):
                self.err("E_TYPE_MISMATCH", "topK query must be a Vector",
                         node)
            scored = []
            for i, v in enumerate(vecs.items):
                if not isinstance(v, VVector) or v.dim != q.dim:
                    self.err("R_VECTOR_DIM",
                             "topK vector %d has wrong dimension" % i, node)
                scored.append((_cosine(q.items, v.items), i))
            scored.sort(key=lambda t: t[0], reverse=True)
            return VList(INT, [i for _, i in scored[:max(k, 0)]])
        # -- Nova Boot ---------------------------------------------
        if name == "router":
            return VRouter()
        if name == "route" or name in ("get", "post", "put", "delete",
                                       "patch"):
            from .web import Route
            _METHODS = {"get": "GET", "post": "POST", "put": "PUT",
                        "delete": "DELETE", "patch": "PATCH"}
            if name == "route":
                r, method, pat, handler = vals[0], vals[1], vals[2], vals[3]
                cap = vals[4]
            else:
                r, pat, handler = vals[0], vals[1], vals[2]
                method = _METHODS[name]
                cap = vals[3]
            if not isinstance(cap, VCapability) or cap.name != "IO":
                self.err("E_CAPABILITY", "route needs an IO capability",
                         node)
            try:
                r.routes.append(Route(method, pat, handler))
            except Exception as e:
                self.err("E_ROUTE", "bad route pattern: %s" % e, node)
            return VUNIT
        if name == "use":
            r, mw = vals[0], vals[1]
            if not isinstance(r, VRouter):
                self.err("E_TYPE_MISMATCH", "use needs a Router", node)
            r.middlewares.append(mw)
            return VUNIT
        if name == "staticDir":
            r, prefix, directory = vals[0], vals[1], vals[2]
            if not isinstance(r, VRouter):
                self.err("E_TYPE_MISMATCH", "staticDir needs a Router",
                         node)
            r.statics.append((prefix, directory))
            return VUNIT
        if name == "serve":
            self.check_fx(IO_EFF, node, "serve")
            r, port = vals[0], vals[1]
            cap = vals[2]
            if not isinstance(r, VRouter):
                self.err("E_TYPE_MISMATCH", "serve needs a Router", node)
            if not isinstance(cap, VCapability) or cap.name != "IO":
                self.err("E_CAPABILITY", "serve needs an IO capability",
                         node)
            from .web import serve_router
            return serve_router(self, r, port, node)
        if name == "response":
            status, body, ctype = vals[0], vals[1], vals[2]
            return VResponse(status, body, ctype)
        if name == "jsonResponse":
            status, v = vals[0], vals[1]
            try:
                return VResponse(status, json_dumps(v),
                                 "application/json")
            except TypeError as e:
                self.err("E_TYPE_MISMATCH", str(e), node)
        if name == "reqMethod":
            return vals[0].method
        if name == "reqPath":
            return vals[0].path
        if name == "reqBody":
            return vals[0].body
        if name == "reqJson":
            import json as _json
            try:
                return VResult(JSON, STRING, True,
                               VJson(_json.loads(vals[0].body)))
            except Exception as e:
                return VResult(JSON, STRING, False,
                               "invalid JSON body: %s" % e)
        if name == "param":
            v = vals[0].path_params.get(vals[1])
            return VOption(STRING, v)
        if name == "paramInt":
            raw = vals[0].typed_params.get(vals[1])
            return VOption(INT, raw if isinstance(raw, int) else None)
        if name == "query":
            v = vals[0].query.get(vals[1])
            return VOption(STRING, v)
        if name == "header":
            v = vals[0].headers.get(vals[1].lower())
            return VOption(STRING, v)
        # -- database ----------------------------------------------
        if name == "dbOpen":
            self.check_fx(IO_EFF, node, "dbOpen")
            cap = vals[1]
            if not isinstance(cap, VCapability) or cap.name != "IO":
                self.err("E_CAPABILITY", "dbOpen needs an IO capability",
                         node)
            from .db import open_db, DbError
            try:
                return VResult(DB, STRING, True, open_db(vals[0]))
            except DbError as e:
                return VResult(DB, STRING, False, str(e))
        if name == "dbTable":
            db, table = vals[0], vals[1]
            if not isinstance(db, VDb):
                self.err("E_TYPE_MISMATCH", "dbTable needs a Db", node)
            return VQuery(db, table)
        if name == "dbExec":
            self.check_fx(IO_EFF, node, "dbExec")
            db, sql = vals[0], vals[1]
            cap = vals[2]
            if not isinstance(cap, VCapability) or cap.name != "IO":
                self.err("E_CAPABILITY", "dbExec needs an IO capability",
                         node)
            from .db import exec_sql, DbError
            try:
                return VResult(INT, STRING, True, exec_sql(db, sql))
            except DbError as e:
                return VResult(INT, STRING, False, str(e))
        self.err("E_INTERNAL", "unknown builtin " + name, node)


def _is_event(name):
    return name.startswith("on") and len(name) > 2 and name[2].isupper()


def _escape_html(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_text(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def format_tree(vnode, indent):
    pad = "  " * indent
    open_tag = "<" + vnode.tag
    for k, v in vnode.attrs:
        open_tag += ' %s="%s"' % (k, v) if v is not None else " %s" % k
    for h in vnode.handlers:
        open_tag += " " + h
    open_tag += ">"
    lines = [pad + open_tag]
    for c in vnode.children:
        if isinstance(c, VNode):
            lines.extend(format_tree(c, indent + 1))
        else:
            lines.append("  " * (indent + 1) + '"%s"' % _escape_text(c))
    lines.append(pad + "</%s>" % vnode.tag)
    return lines


def format_html(vnode):
    parts = ["<" + vnode.tag]
    for k, v in vnode.attrs:
        parts.append(' %s="%s"' % (k, _escape_html(v)) if v is not None
                     else " " + k)
    parts.append(">")
    for c in vnode.children:
        parts.append(format_html(c) if isinstance(c, VNode)
                     else _escape_html(c))
    parts.append("</%s>" % vnode.tag)
    return "".join(parts)


def _query_scalar(v, node, interp):
    """Convert a Nova where-value to a sqlite parameter."""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, VOption):
        return None if v.value is None else _query_scalar(v.value, node,
                                                          interp)
    interp.err("E_TYPE_MISMATCH",
               "where value must be Int/Float/String/Bool", node)


def _cosine(a, b):
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mock_embedding(prompt, dim):
    """Deterministic pseudo-embedding: sha256 stream -> [-1, 1] floats,
    L2-normalized. Stable across runs (no hash randomization)."""
    import hashlib
    import math
    out = []
    counter = 0
    while len(out) < dim:
        h = hashlib.sha256(("%d:%s" % (counter, prompt)).encode("utf-8"))
        digest = h.digest()
        for i in range(0, len(digest), 4):
            if len(out) >= dim:
                break
            word = int.from_bytes(digest[i:i + 4], "big")
            out.append((word / 2 ** 32) * 2 - 1)
        counter += 1
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


