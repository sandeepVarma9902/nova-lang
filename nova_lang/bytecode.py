# bytecode.py — NovaVM bytecode: opcodes, compiler, assembler/disassembler,
# and the .nvb binary file format.
#
# NovaVM is the "write once, run anywhere" story from SPEC.md section 18:
# the tree-walking interpreter is the reference semantics; this module
# compiles the portable SUBSET of Nova (see docs/v2-bytecode.md) to a
# compact, versioned bytecode that any NovaVM implementation can execute.
#
# This particular VM (vm.py) is a Python dispatch loop — the portability
# story, not a speed story. See docs/v2-bytecode.md for honest limits.
#
# Layout of a compiled Program:
#   Program.functions  -- flat list of Code objects (index 0 is <main>)
#   Program.entry      -- index of the entry function (always 0)
#   Code.consts        -- per-function constant pool
#   Code.code          -- bytes; every instruction is exactly 3 bytes:
#                         u8 opcode, u16 big-endian operand
#
# Variable model: each function frame owns a flat cell array. A variable
# reference compiles to (depth, slot): depth = how many function frames
# to walk outward, slot = cell index in that frame. Block scopes share
# their function's cells (slots are allocated per function), so closures
# over locals, globals, and loop variables all work by construction.

import os
import struct
from enum import IntEnum

from . import ast as A
from .lexer import Lexer, LexError
from .parser import Parser, ParseError, JsxError
from .checker import check_source

INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1

MAGIC = b"NVB1"
FORMAT_VERSION = 1


class CompileError(Exception):
    """Raised when Nova source cannot be compiled to NovaVM bytecode."""

    def __init__(self, message, line=0, col=0):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col

    def __str__(self):
        if self.line:
            return "line %d: %s" % (self.line, self.message)
        return self.message


# ---------------------------------------------------------------- opcodes ----
# Keep this small and stable: the .nvb format versions the opcode numbers.
class Op(IntEnum):
    LOAD_CONST = 1       # push consts[operand]
    POP = 2              # discard top of stack
    DUP = 3              # duplicate top of stack

    LOAD_VAR = 4         # operand: depth<<8 | slot — push frame cell
    STORE_VAR = 5        # operand: depth<<8 | slot — pop into frame cell

    ADD = 6              # Int+Int (checked) | Float+Float | String+String
    SUB = 7
    MUL = 8
    DIV = 9              # Int division truncates toward zero (checked)
    MOD = 10
    NEG = 11             # unary minus (checked for Int)
    NOT = 12             # boolean not

    EQ = 13
    NE = 14
    LT = 15
    LE = 16
    GT = 17
    GE = 18

    JUMP = 19            # operand: target instruction index
    JUMP_IF_FALSE = 20   # pop; jump if popped value is False
    JUMP_IF_TRUE = 21    # pop; jump if popped value is True

    MAKE_FUNCTION = 22   # push closure over functions[operand], capturing
                         # the current frame
    CALL = 23            # operand: argc — pop callee + args, call
    RETURN = 24          # pop value, return from current function

    BUILD_LIST = 25      # operand: n — pop n items, push list
    MAKE_VARIANT = 26    # operand: 0=Some(v) 1=None 2=Ok(v) 3=Err(v)
    INDEX = 27           # pop index, pop list -> push element
    STORE_INDEX = 28     # pop value, pop index, pop list; store; push value
    TAG_OF = 29          # pop v, push tag: 0=None 1=Some 2=Ok 3=Err 4=other
    VARIANT_VALUE = 30   # pop Some/Ok/Err, push the inner value
    UNWRAP_OR_RETURN = 31  # the `?` operator: unwrap Ok/Some, or return
                           # the Err/None out of the current function

    LEN = 32             # pop list, push length
    LIST_PUSH = 33       # pop value, pop list; append; push ()
    UNWRAP_MSG = 34      # pop msg, pop variant -> inner value or runtime err

    PRINT = 35           # operand: argc — pop args, print, push ()
    STR = 36             # pop v, push str(v)
    INT_OF = 37          # pop v, push int(v) truncated + range-checked
    ASSERT = 38          # operand: argc (1|2) — pop cond[, msg]; check
    FAIL = 39            # operand: const index of message — compiler trap


# ------------------------------------------------------------------- code ----
class _Unit:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "()"


UNIT = _Unit()  # the () value, used only inside const pools


class Code:
    """One compiled function."""

    def __init__(self, name, param_names):
        self.name = name
        self.param_names = list(param_names)
        self.consts = []          # int | float | str | bool | UNIT
        self._const_ids = {}
        self.instrs = []          # list of [op:int, operand:int, line:int]
        self.nslots = 0

    def add_const(self, value):
        if isinstance(value, bool):
            key = ("bool", value)
        elif isinstance(value, int):
            key = ("int", value)
        elif isinstance(value, float):
            key = ("float", value)
        elif isinstance(value, str):
            key = ("str", value)
        elif value is UNIT:
            key = ("unit", 0)
        else:
            raise CompileError("bad const %r" % (value,))
        if key in self._const_ids:
            return self._const_ids[key]
        idx = len(self.consts)
        if idx > 0xFFFF:
            raise CompileError("too many constants in '%s'" % self.name)
        self.consts.append(value)
        self._const_ids[key] = idx
        return idx


class Program:
    """A complete compiled program: flat function table + entry index."""

    def __init__(self, functions, entry=0, source=""):
        self.functions = functions
        self.entry = entry
        self.source = source


# --------------------------------------------------------------- compiler ----
# Out of scope for the NovaVM backend — rejected with a clear error so
# nobody mistakes "compiled" for "runs".
_OUT_OF_SCOPE = (
    A.Import, A.ClassDecl, A.InterfaceDecl, A.ComponentDecl, A.AiDecl,
    A.StreamDecl, A.ContextDecl, A.Spawn, A.SpawnAsync, A.Nursery,
    A.Provide, A.Consume, A.This, A.Super, A.SuperCall, A.Field,
    A.ResultVar, A.JElement, A.JText, A.JExpr, A.JFor,
)

# Builtins lowered to dedicated opcodes.
_SIMPLE_BUILTINS = {"print": Op.PRINT, "str": Op.STR, "int": Op.INT_OF,
                    "len": Op.LEN, "assert": Op.ASSERT}
# Builtins that need capabilities / runtimes the VM does not have.
_BLOCKED_BUILTINS = {
    "readFile": "file IO needs an IO capability",
    "writeFile": "file IO needs an IO capability",
    "fetch": "network IO needs a Net capability",
    "secret": "the secret store is a platform service",
    "prompt": "AI calls are not supported by NovaVM",
    "sleep": "the event loop is not supported by NovaVM",
    "render": "components are not supported by NovaVM",
}


class _Scope:
    __slots__ = ("parent", "func_depth", "names")

    def __init__(self, parent, func_depth):
        self.parent = parent
        self.func_depth = func_depth
        self.names = {}  # name -> slot


class _Fn:
    __slots__ = ("code", "func_depth", "scope", "labels")

    def __init__(self, code, func_depth, parent_scope):
        self.code = code
        self.func_depth = func_depth
        # The base scope links to the enclosing function's scope so
        # name resolution walks the true lexical chain.
        self.scope = _Scope(parent_scope, func_depth)
        self.labels = []  # label index -> instruction index (per function)


class Compiler:
    """AST -> Program. One Compiler compiles one program."""

    def __init__(self, filename=""):
        self.filename = filename
        self.functions = []
        self._fns = []          # stack of _Fn
        self._fndecls = []      # stack of {name: [param names]}, parallel
                                # to scopes — for named-arg reordering
        self._line = 0
        self._labels = None     # per-function label patch list

    # -- function / scope bookkeeping ---------------------------------
    def _cur(self):
        return self._fns[-1]

    def _push_fn(self, code):
        if self._fns:
            parent = self._cur()
            depth = parent.func_depth + 1
            self._fns.append(_Fn(code, depth, parent.scope))
        else:
            self._fns.append(_Fn(code, 0, None))

    def _pop_fn(self):
        fn = self._fns.pop()
        self._patch_labels(fn)

    def _push_scope(self):
        cur = self._cur()
        cur.scope = _Scope(cur.scope, cur.func_depth)
        self._fndecls.append({})

    def _pop_scope(self):
        self._cur().scope = self._cur().scope.parent
        self._fndecls.pop()

    def _declare(self, name, node):
        scope = self._cur().scope
        if name in scope.names:
            raise CompileError("duplicate declaration of '%s'" % name,
                               node.line, node.col)
        slot = self._cur().code.nslots
        if slot > 0xFF:
            raise CompileError("too many locals in '%s' (max 255)"
                               % self._cur().code.name, node.line, node.col)
        self._cur().code.nslots += 1
        scope.names[name] = slot
        return slot

    def _resolve(self, name, node):
        cur = self._cur()
        scope = cur.scope
        while scope is not None:
            if name in scope.names:
                depth = cur.func_depth - scope.func_depth
                if depth > 0xFF:
                    raise CompileError(
                        "variable '%s' is too deeply nested" % name,
                        node.line, node.col)
                return (depth << 8) | scope.names[name]
            scope = scope.parent
        raise CompileError("undefined name '%s'" % name, node.line, node.col)

    def _known_fn_params(self, name):
        for d in reversed(self._fndecls):
            if name in d:
                return d[name]
        return None

    # -- emission ------------------------------------------------------
    def _emit(self, op, operand=0):
        if operand < 0 or operand > 0xFFFF:
            raise CompileError("operand out of range for %s" % op.name,
                               self._line, 0)
        self._cur().code.instrs.append([int(op), operand, self._line])

    def _new_label(self):
        self._cur().labels.append(None)
        return len(self._cur().labels) - 1

    def _mark(self, label):
        self._cur().labels[label] = len(self._cur().code.instrs)

    def _emit_jump(self, op, label):
        self._cur().code.instrs.append([int(op), label, self._line])

    def _patch_labels(self, fn):
        code = fn.code
        for ins in code.instrs:
            op = ins[0]
            if op in (Op.JUMP, Op.JUMP_IF_FALSE, Op.JUMP_IF_TRUE):
                label = ins[1]
                target = fn.labels[label]
                if target is None:
                    raise CompileError("unresolved label in '%s'"
                                       % code.name)
                if target > 0xFFFF:
                    raise CompileError("function '%s' too large"
                                       % code.name)
                ins[1] = target

    def _const(self, value):
        return self._cur().code.add_const(value)

    def _load_unit(self):
        self._emit(Op.LOAD_CONST, self._const(UNIT))

    def _fail(self, node, message):
        self._line = node.line
        raise CompileError(message, node.line, node.col)

    # -- entry ----------------------------------------------------------
    def compile_program(self, prog):
        main = Code("<main>", [])
        self.functions.append(main)
        self._push_fn(main)
        self._push_scope()
        # Hoist top-level functions: the interpreter registers every
        # top-level fn before running any statement, so `f()` works even
        # when `fn f` is declared later in the file.
        for stmt in prog.stmts:
            if isinstance(stmt, A.FnDecl):
                self._declare_fn_name(stmt)
            elif isinstance(stmt, (A.LetDecl, A.VarDecl)):
                self._declare(stmt.name, stmt)
        for stmt in prog.stmts:
            if isinstance(stmt, A.FnDecl):
                self._compile_fn_decl(stmt)
        for stmt in prog.stmts:
            if not isinstance(stmt, A.FnDecl):
                self._compile_stmt(stmt)
        self._line = 0
        self._load_unit()
        self._emit(Op.RETURN)
        self._pop_scope()
        self._pop_fn()
        return Program(self.functions, entry=0, source=self.filename)

    def _declare_fn_name(self, node):
        self._check_fn_ok(node)
        self._declare(node.name, node)
        self._fndecls[-1][node.name] = [p.name for p in node.params]

    def _check_fn_ok(self, node):
        if node.effect not in ("pure", "effect"):
            self._fail(node, "NovaVM: '%s' has effect '%s' — capabilities, "
                             "async and the event loop are not supported; "
                             "run it with the interpreter"
                       % (node.name, node.effect))
        if node.requires or node.ensures:
            self._fail(node, "NovaVM: contracts (requires/ensures) on '%s' "
                             "are not supported; remove them or run with "
                             "the interpreter" % node.name)

    def _compile_fn_decl(self, node):
        self._line = node.line
        code = Code(node.name, [p.name for p in node.params])
        fidx = len(self.functions)
        self.functions.append(code)
        self._push_fn(code)
        self._push_scope()
        for p in node.params:
            self._declare(p.name, p)
        # A body that falls off the end returns () — same as the
        # interpreter (exec_stmt without ReturnExc -> VUNIT).
        self._compile_block_stmts(node.body.stmts, want_value=False)
        self._load_unit()
        self._emit(Op.RETURN)
        self._pop_scope()
        self._pop_fn()
        self._emit(Op.MAKE_FUNCTION, fidx)
        self._emit(Op.STORE_VAR, self._resolve(node.name, node))

    # -- statements -----------------------------------------------------
    def _compile_stmt(self, node):
        saved = self._line
        self._line = node.line
        try:
            self._compile_stmt_inner(node)
        finally:
            self._line = saved

    def _compile_stmt_inner(self, node):
        if isinstance(node, (A.LetDecl, A.VarDecl)):
            if node.expr is None:
                self._fail(node, "NovaVM: '%s' needs an initializer"
                           % node.name)
            self._compile_expr(node.expr)
            self._emit(Op.STORE_VAR, self._resolve(node.name, node))
        elif isinstance(node, A.ExprStmt):
            self._compile_expr(node.expr)
            self._emit(Op.POP)
        elif isinstance(node, A.Return):
            if node.expr is not None:
                self._compile_expr(node.expr)
            else:
                self._load_unit()
            self._emit(Op.RETURN)
        elif isinstance(node, A.IfStmt):
            self._compile_if_stmt(node)
        elif isinstance(node, A.WhileStmt):
            self._compile_while(node)
        elif isinstance(node, A.ForStmt):
            self._compile_for(node)
        elif isinstance(node, A.FnDecl):
            self._compile_fn_decl(node)
        elif isinstance(node, A.Block):
            self._compile_block_stmts(node.stmts, want_value=False)
        elif isinstance(node, _OUT_OF_SCOPE):
            self._fail(node, "NovaVM: %s is not supported by the bytecode "
                             "backend — run with the interpreter"
                       % type(node).__name__)
        else:
            self._fail(node, "NovaVM: statement %s is not supported"
                       % type(node).__name__)

    def _compile_block_stmts(self, stmts, want_value):
        """Compile a statement list. If want_value, the block leaves its
        value on the stack (value of the last ExprStmt, else ())."""
        self._push_scope()
        for s in stmts:
            if isinstance(s, A.FnDecl):
                self._declare_fn_name(s)
            elif isinstance(s, (A.LetDecl, A.VarDecl)):
                self._declare(s.name, s)
        last = len(stmts) - 1
        for i, s in enumerate(stmts):
            if want_value and i == last and isinstance(s, A.ExprStmt):
                self._line = s.line
                self._compile_expr(s.expr)  # value stays on the stack
            else:
                self._compile_stmt(s)
                if want_value and i == last:
                    # last statement was not an ExprStmt -> value is ()
                    self._load_unit()
        if want_value and not stmts:
            self._load_unit()
        self._pop_scope()

    def _compile_if_stmt(self, node):
        self._compile_expr(node.cond)
        l_else = self._new_label()
        l_end = self._new_label()
        self._emit_jump(Op.JUMP_IF_FALSE, l_else)
        self._compile_block_stmts(node.then_b.stmts, want_value=False)
        if node.else_b is not None:
            self._emit_jump(Op.JUMP, l_end)
            self._mark(l_else)
            if isinstance(node.else_b, A.IfStmt):
                self._compile_if_stmt(node.else_b)
            else:
                self._compile_block_stmts(node.else_b.stmts,
                                          want_value=False)
            self._mark(l_end)
        else:
            self._mark(l_else)

    def _compile_while(self, node):
        l_head = self._new_label()
        l_end = self._new_label()
        self._mark(l_head)
        self._compile_expr(node.cond)
        self._emit_jump(Op.JUMP_IF_FALSE, l_end)
        self._compile_block_stmts(node.body.stmts, want_value=False)
        self._emit_jump(Op.JUMP, l_head)
        self._mark(l_end)

    def _compile_for(self, node):
        # Desugared: evaluate the iterable once, walk it by index.
        # (The interpreter iterates the live list; indexing each step
        # observes appends the same way.)
        self._push_scope()
        it_slot = self._declare("#it", node)
        ix_slot = self._declare("#ix", node)
        var_slot = self._declare(node.var, node)
        self._compile_expr(node.iter)
        self._emit(Op.STORE_VAR, it_slot)
        self._emit(Op.LOAD_CONST, self._const(0))
        self._emit(Op.STORE_VAR, ix_slot)
        l_head = self._new_label()
        l_end = self._new_label()
        self._mark(l_head)
        self._emit(Op.LOAD_VAR, ix_slot)
        self._emit(Op.LOAD_VAR, it_slot)
        self._emit(Op.LEN)
        self._emit(Op.LT)
        self._emit_jump(Op.JUMP_IF_FALSE, l_end)
        self._emit(Op.LOAD_VAR, it_slot)
        self._emit(Op.LOAD_VAR, ix_slot)
        self._emit(Op.INDEX)
        self._emit(Op.STORE_VAR, var_slot)
        self._compile_block_stmts(node.body.stmts, want_value=False)
        self._emit(Op.LOAD_VAR, ix_slot)
        self._emit(Op.LOAD_CONST, self._const(1))
        self._emit(Op.ADD)
        self._emit(Op.STORE_VAR, ix_slot)
        self._emit_jump(Op.JUMP, l_head)
        self._mark(l_end)
        self._pop_scope()

    # -- expressions ------------------------------------------------------
    def _compile_expr(self, node):
        saved = self._line
        self._line = node.line
        try:
            self._compile_expr_inner(node)
        finally:
            self._line = saved

    def _compile_expr_inner(self, node):
        if isinstance(node, A.IntLit):
            if node.value < INT_MIN or node.value > INT_MAX:
                self._fail(node, "integer literal out of 64-bit range")
            self._emit(Op.LOAD_CONST, self._const(node.value))
        elif isinstance(node, A.FloatLit):
            self._emit(Op.LOAD_CONST, self._const(node.value))
        elif isinstance(node, A.StrLit):
            self._emit(Op.LOAD_CONST, self._const(node.value))
        elif isinstance(node, A.BoolLit):
            self._emit(Op.LOAD_CONST, self._const(node.value))
        elif isinstance(node, A.NoneLit):
            self._emit(Op.MAKE_VARIANT, 1)
        elif isinstance(node, A.SomeExpr):
            self._compile_expr(node.expr)
            self._emit(Op.MAKE_VARIANT, 0)
        elif isinstance(node, A.OkExpr):
            self._compile_expr(node.expr)
            self._emit(Op.MAKE_VARIANT, 2)
        elif isinstance(node, A.ErrExpr):
            self._compile_expr(node.expr)
            self._emit(Op.MAKE_VARIANT, 3)
        elif isinstance(node, A.ListLit):
            for e in node.elems:
                self._compile_expr(e)
            self._emit(Op.BUILD_LIST, len(node.elems))
        elif isinstance(node, A.Var):
            self._emit(Op.LOAD_VAR, self._resolve(node.name, node))
        elif isinstance(node, A.Binary):
            self._compile_binary(node)
        elif isinstance(node, A.Unary):
            self._compile_expr(node.expr)
            if node.op == "not":
                self._emit(Op.NOT)
            elif node.op == "-":
                self._emit(Op.NEG)
            else:
                self._fail(node, "unknown unary operator '%s'" % node.op)
        elif isinstance(node, A.Assign):
            self._compile_assign(node)
        elif isinstance(node, A.Call):
            self._compile_call(node)
        elif isinstance(node, A.MethodCall):
            self._compile_method_call(node)
        elif isinstance(node, A.Index):
            self._compile_expr(node.obj)
            self._compile_expr(node.index)
            self._emit(Op.INDEX)
        elif isinstance(node, A.Question):
            self._compile_expr(node.expr)
            self._emit(Op.UNWRAP_OR_RETURN)
        elif isinstance(node, A.IfExpr):
            self._compile_if_expr(node)
        elif isinstance(node, A.Match):
            self._compile_match(node)
        elif isinstance(node, _OUT_OF_SCOPE):
            self._fail(node, "NovaVM: %s is not supported by the bytecode "
                             "backend — run with the interpreter"
                       % type(node).__name__)
        else:
            self._fail(node, "NovaVM: expression %s is not supported"
                       % type(node).__name__)

    _BIN_OPS = {"+": Op.ADD, "-": Op.SUB, "*": Op.MUL, "/": Op.DIV,
                "%": Op.MOD, "==": Op.EQ, "!=": Op.NE, "<": Op.LT,
                "<=": Op.LE, ">": Op.GT, ">=": Op.GE}

    def _compile_binary(self, node):
        if node.op == "and":
            # short-circuit; result is a real Bool like the interpreter's
            self._compile_expr(node.left)
            l_false = self._new_label()
            l_end = self._new_label()
            self._emit_jump(Op.JUMP_IF_FALSE, l_false)
            self._compile_expr(node.right)
            self._emit_jump(Op.JUMP, l_end)
            self._mark(l_false)
            self._emit(Op.LOAD_CONST, self._const(False))
            self._mark(l_end)
            return
        if node.op == "or":
            self._compile_expr(node.left)
            l_true = self._new_label()
            l_end = self._new_label()
            self._emit_jump(Op.JUMP_IF_TRUE, l_true)
            self._compile_expr(node.right)
            self._emit_jump(Op.JUMP, l_end)
            self._mark(l_true)
            self._emit(Op.LOAD_CONST, self._const(True))
            self._mark(l_end)
            return
        if node.op not in self._BIN_OPS:
            self._fail(node, "unknown binary operator '%s'" % node.op)
        self._compile_expr(node.left)
        self._compile_expr(node.right)
        self._emit(self._BIN_OPS[node.op])

    def _compile_assign(self, node):
        target = node.target
        if isinstance(target, A.Var):
            # Assignment is an expression: the value stays on the stack.
            self._compile_expr(node.value)
            self._emit(Op.DUP)
            self._emit(Op.STORE_VAR, self._resolve(target.name, target))
        elif isinstance(target, A.Index):
            # STORE_INDEX pops value/index/obj and pushes the value back,
            # so the assignment expression's value is already on the stack.
            self._compile_expr(target.obj)
            self._compile_expr(target.index)
            self._compile_expr(node.value)
            self._emit(Op.STORE_INDEX)
        else:
            self._fail(node, "NovaVM: cannot assign to %s"
                       % type(target).__name__)

    def _compile_call(self, node):
        func = node.func
        if isinstance(func, A.Var) and func.name in _BLOCKED_BUILTINS:
            self._fail(node, "NovaVM: '%s' is not supported — %s"
                       % (func.name, _BLOCKED_BUILTINS[func.name]))
        if isinstance(func, A.Var) and func.name in _SIMPLE_BUILTINS:
            self._compile_builtin(node, func.name)
            return
        if not isinstance(func, A.Var):
            self._fail(node, "NovaVM: only direct calls are supported")
        # Named arguments are reordered at compile time against the
        # callee's declared parameter order. Indirect calls (through a
        # variable holding a function) with named arguments are rejected:
        # the VM has no runtime parameter metadata.
        params = self._known_fn_params(func.name)
        ordered = self._order_args(node, params)
        for _, expr in ordered:
            self._compile_expr(expr)
        self._emit(Op.LOAD_VAR, self._resolve(func.name, func))
        self._emit(Op.CALL, len(ordered))

    def _order_args(self, node, params):
        args = node.args
        if not any(n is not None for n, _ in args):
            return list(args)
        if params is None:
            self._fail(node, "NovaVM: named arguments need a statically "
                             "known function; '%s' is not one"
                       % node.func.name)
        values = {}
        pos = 0
        for aname, expr in args:
            if aname is not None:
                if aname not in params:
                    self._fail(node, "'%s' has no parameter '%s'"
                               % (node.func.name, aname))
                if aname in values:
                    self._fail(node, "duplicate argument '%s'" % aname)
                values[aname] = expr
            else:
                if pos >= len(params):
                    self._fail(node, "'%s' takes %d argument(s)"
                               % (node.func.name, len(params)))
                pname = params[pos]
                pos += 1
                if pname in values:
                    self._fail(node, "duplicate argument '%s'" % pname)
                values[pname] = expr
        missing = [p for p in params if p not in values]
        if missing:
            self._fail(node, "'%s' missing arguments: %s"
                       % (node.func.name, ", ".join(missing)))
        return [(None, values[p]) for p in params]

    def _compile_builtin(self, node, name):
        argc = len(node.args)
        if any(n is not None for n, _ in node.args):
            self._fail(node, "NovaVM: builtin '%s' takes positional "
                             "arguments only" % name)
        want = {"print": None, "str": 1, "int": 1, "len": 1, "assert": (1, 2)}
        w = want[name]
        if w is None:
            pass
        elif isinstance(w, tuple):
            if argc not in w:
                self._fail(node, "'%s' takes 1 or 2 arguments" % name)
        elif argc != w:
            self._fail(node, "'%s' takes %d argument(s)" % (name, w))
        for _, expr in node.args:
            self._compile_expr(expr)
        op = _SIMPLE_BUILTINS[name]
        if name == "print":
            self._emit(op, argc)
        elif name == "assert":
            self._emit(op, argc)
        else:
            self._emit(op)

    def _compile_method_call(self, node):
        name = node.name
        if name == "push":
            if len(node.args) != 1 or node.args[0][0] is not None:
                self._fail(node, "push takes 1 positional argument")
            self._compile_expr(node.obj)
            self._compile_expr(node.args[0][1])
            self._emit(Op.LIST_PUSH)
            return
        if name in ("unwrap", "expect"):
            self._compile_expr(node.obj)
            if name == "expect":
                if len(node.args) != 1 or node.args[0][0] is not None:
                    self._fail(node, "expect takes 1 positional argument")
                self._compile_expr(node.args[0][1])
            else:
                if node.args:
                    self._fail(node, "unwrap takes no arguments")
                self._load_unit()  # no message -> default text
            self._emit(Op.UNWRAP_MSG)
            return
        self._fail(node, "NovaVM: method '%s' is not supported" % name)

    def _compile_if_expr(self, node):
        if node.else_b is None:
            self._fail(node, "NovaVM: if-expressions need an else branch")
        self._compile_expr(node.cond)
        l_else = self._new_label()
        l_end = self._new_label()
        self._emit_jump(Op.JUMP_IF_FALSE, l_else)
        self._compile_block_stmts(node.then_b.stmts, want_value=True)
        self._emit_jump(Op.JUMP, l_end)
        self._mark(l_else)
        self._compile_block_stmts(node.else_b.stmts, want_value=True)
        self._mark(l_end)

    def _compile_match(self, node):
        self._compile_expr(node.scrutinee)  # [scr]
        l_end = self._new_label()
        for arm in node.arms:
            l_next = self._new_label()
            pat = arm.pattern
            self._push_scope()
            bound = []
            if isinstance(pat, A.PatWild):
                pass
            elif isinstance(pat, A.PatVar):
                bound.append(pat.name)
            elif isinstance(pat, (A.PatSome, A.PatOk, A.PatErr)):
                tag = {A.PatSome: 1, A.PatOk: 2, A.PatErr: 3}[type(pat)]
                self._emit(Op.DUP)
                self._emit(Op.TAG_OF)
                self._emit(Op.LOAD_CONST, self._const(tag))
                self._emit(Op.EQ)
                self._emit_jump(Op.JUMP_IF_FALSE, l_next)
                bound.append(pat.name)
            elif isinstance(pat, A.PatNone):
                self._emit(Op.DUP)
                self._emit(Op.TAG_OF)
                self._emit(Op.LOAD_CONST, self._const(0))
                self._emit(Op.EQ)
                self._emit_jump(Op.JUMP_IF_FALSE, l_next)
            elif isinstance(pat, A.PatLit):
                if not isinstance(pat.value, (bool, int, float, str)):
                    self._fail(arm, "NovaVM: match literal must be a "
                                    "Bool, Int, Float or String")
                self._emit(Op.DUP)
                self._emit(Op.LOAD_CONST, self._const(pat.value))
                self._emit(Op.EQ)
                self._emit_jump(Op.JUMP_IF_FALSE, l_next)
            else:
                self._pop_scope()
                self._fail(arm, "NovaVM: match pattern %s is not supported"
                           % type(pat).__name__)
            # matched: bind (each arm gets its own scope)
            for b in bound:
                slot = self._cur().code.nslots
                self._cur().code.nslots += 1
                self._cur().scope.names[b] = slot
            if isinstance(pat, (A.PatSome, A.PatOk, A.PatErr)):
                self._emit(Op.VARIANT_VALUE)
                self._emit(Op.STORE_VAR,
                           (0 << 8) | self._cur().scope.names[bound[0]])
            elif isinstance(pat, A.PatVar):
                self._emit(Op.STORE_VAR,
                           (0 << 8) | self._cur().scope.names[bound[0]])
            else:
                self._emit(Op.POP)  # discard the scrutinee
            self._compile_expr(arm.expr)
            self._pop_scope()
            self._emit_jump(Op.JUMP, l_end)
            self._mark(l_next)
        # The checker proves exhaustiveness, so this is unreachable in
        # checked programs — a trap, not a code path.
        self._emit(Op.POP)
        self._emit(Op.FAIL, self._const("no match arm matched"))
        self._mark(l_end)


# ------------------------------------------------------------ file format ----
# .nvb — NovaVM binary. All integers big-endian.
#
#   magic          4s  "NVB1"
#   version        u16 FORMAT_VERSION (1)
#   source_len     u16, source bytes (utf-8, informational)
#   nfuncs         u32
#   per function:
#     name_len     u16, name bytes
#     nparams      u16
#     nslots       u16
#     nconsts      u32
#     per const:   u8 tag + payload
#                    0 = unit
#                    1 = bool, u8
#                    2 = int, i64
#                    3 = float, f64
#                    4 = str, u32 len + bytes
#     code_len     u32 (bytes; always a multiple of 3)
#     code         bytes (u8 op, u16 operand per instruction)
#     nlines       u32 (= instruction count)
#     lines        u16 per instruction (source line, 0 = unknown)
#   entry          u32 (function index)

_CONST_TAGS = {0: "unit", 1: "bool", 2: "int", 3: "float", 4: "str"}


def _encode_const(value):
    if value is UNIT:
        return struct.pack(">B", 0)
    if isinstance(value, bool):
        return struct.pack(">BB", 1, 1 if value else 0)
    if isinstance(value, int):
        return struct.pack(">Bq", 2, value)
    if isinstance(value, float):
        return struct.pack(">Bd", 3, value)
    if isinstance(value, str):
        b = value.encode("utf-8")
        return struct.pack(">BI", 4, len(b)) + b
    raise CompileError("cannot encode const %r" % (value,))


def _decode_const(buf, pos):
    tag = buf[pos]
    pos += 1
    if tag == 0:
        return UNIT, pos
    if tag == 1:
        v = buf[pos] != 0
        return v, pos + 1
    if tag == 2:
        return struct.unpack_from(">q", buf, pos)[0], pos + 8
    if tag == 3:
        return struct.unpack_from(">d", buf, pos)[0], pos + 8
    if tag == 4:
        (ln,) = struct.unpack_from(">I", buf, pos)
        pos += 4
        return buf[pos:pos + ln].decode("utf-8"), pos + ln
    raise CompileError("bad const tag %d in .nvb" % tag)


def _encode_str(s):
    b = s.encode("utf-8")
    if len(b) > 0xFFFF:
        raise CompileError("string too long for .nvb")
    return struct.pack(">H", len(b)) + b


def _decode_str(buf, pos):
    (ln,) = struct.unpack_from(">H", buf, pos)
    pos += 2
    return buf[pos:pos + ln].decode("utf-8"), pos + ln


def dump(program):
    """Serialize a Program to .nvb bytes."""
    out = bytearray()
    out += MAGIC
    out += struct.pack(">H", FORMAT_VERSION)
    out += _encode_str(program.source or "")
    out += struct.pack(">I", len(program.functions))
    for code in program.functions:
        out += _encode_str(code.name)
        out += struct.pack(">HH", len(code.param_names), code.nslots)
        out += struct.pack(">I", len(code.consts))
        for c in code.consts:
            out += _encode_const(c)
        encoded = bytearray()
        lines = []
        for op, operand, line in code.instrs:
            encoded += struct.pack(">BH", op, operand)
            lines.append(line if line <= 0xFFFF else 0)
        out += struct.pack(">I", len(encoded))
        out += bytes(encoded)
        out += struct.pack(">I", len(lines))
        for ln in lines:
            out += struct.pack(">H", ln)
        # param names are positional; nothing to store
    out += struct.pack(">I", program.entry)
    return bytes(out)


def load(data):
    """Deserialize .nvb bytes to a Program."""
    pos = 0
    if data[:4] != MAGIC:
        raise CompileError("not a NovaVM bytecode file (bad magic)")
    pos = 4
    (ver,) = struct.unpack_from(">H", data, pos)
    pos += 2
    if ver != FORMAT_VERSION:
        raise CompileError("unsupported .nvb version %d" % ver)
    source, pos = _decode_str(data, pos)
    (nfuncs,) = struct.unpack_from(">I", data, pos)
    pos += 4
    functions = []
    for _ in range(nfuncs):
        name, pos = _decode_str(data, pos)
        nparams, nslots = struct.unpack_from(">HH", data, pos)
        pos += 4
        (nconsts,) = struct.unpack_from(">I", data, pos)
        pos += 4
        code = Code(name, ["#%d" % i for i in range(nparams)])
        for _ in range(nconsts):
            c, pos = _decode_const(data, pos)
            code.consts.append(c)
        (code_len,) = struct.unpack_from(">I", data, pos)
        pos += 4
        raw = data[pos:pos + code_len]
        pos += code_len
        if len(raw) != code_len or code_len % 3 != 0:
            raise CompileError("truncated code in .nvb")
        (nlines,) = struct.unpack_from(">I", data, pos)
        pos += 4
        lines = []
        for _ in range(nlines):
            (ln,) = struct.unpack_from(">H", data, pos)
            pos += 2
            lines.append(ln)
        code.nslots = nslots
        for i in range(0, code_len, 3):
            op, operand = struct.unpack_from(">BH", raw, i)
            try:
                Op(op)
            except ValueError:
                raise CompileError("unknown opcode %d in .nvb" % op)
            code.instrs.append([op, operand,
                                lines[i // 3] if i // 3 < len(lines) else 0])
        functions.append(code)
    (entry,) = struct.unpack_from(">I", data, pos)
    if entry >= len(functions):
        raise CompileError("bad entry index in .nvb")
    return Program(functions, entry, source)


# ------------------------------------------------- text assembly (debug) ----
# Human-readable assembly for debugging. Round-trips: disassemble ->
# assemble -> identical Program (modulo param names, which are not
# semantically significant).

def _escape_str(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif 32 <= o < 127:
            out.append(ch)
        else:
            out.append("\\u%04x" % o)
    return "".join(out)


def _unescape_str(s):
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        e = s[i]
        i += 1
        if e == "n":
            out.append("\n")
        elif e == "t":
            out.append("\t")
        elif e == "r":
            out.append("\r")
        elif e == "\\":
            out.append("\\")
        elif e == '"':
            out.append('"')
        elif e == "u":
            out.append(chr(int(s[i:i + 4], 16)))
            i += 4
        else:
            raise CompileError("bad escape '\\%s' in assembly" % e)
    return "".join(out)


def _fmt_const(c):
    if c is UNIT:
        return "unit"
    if isinstance(c, bool):
        return "bool %s" % ("true" if c else "false")
    if isinstance(c, int):
        return "int %d" % c
    if isinstance(c, float):
        return "float %r" % c
    return 'str "%s"' % _escape_str(c)


def _parse_const(text):
    text = text.strip()
    if text == "unit":
        return UNIT
    if text.startswith("bool "):
        v = text[5:].strip()
        if v == "true":
            return True
        if v == "false":
            return False
        raise CompileError("bad bool const '%s'" % text)
    if text.startswith("int "):
        return int(text[4:].strip())
    if text.startswith("float "):
        return float(text[6:].strip())
    if text.startswith("str "):
        body = text[4:].strip()
        if len(body) >= 2 and body[0] == '"' and body[-1] == '"':
            return _unescape_str(body[1:-1])
        raise CompileError("bad str const '%s'" % text)
    raise CompileError("bad const '%s'" % text)


def disassemble(program):
    """Render a Program as human-readable assembly text."""
    lines = []
    lines.append("# NovaVM assembly — source: %s" %
                 (program.source or "<unknown>"))
    lines.append("# %d function(s), entry=%d"
                 % (len(program.functions), program.entry))
    for fi, code in enumerate(program.functions):
        lines.append("")
        lines.append(".func %s params=%d slots=%d  # [%d]"
                     % (code.name, len(code.param_names), code.nslots, fi))
        for i, c in enumerate(code.consts):
            lines.append(".const %d = %s" % (i, _fmt_const(c)))
        # labels at every jump target
        targets = set()
        for op, operand, _ in code.instrs:
            if op in (Op.JUMP, Op.JUMP_IF_FALSE, Op.JUMP_IF_TRUE):
                targets.add(operand)
        label_names = {}
        for n, t in enumerate(sorted(targets)):
            label_names[t] = "L%d" % n
        for idx, (op, operand, line) in enumerate(code.instrs):
            if idx in label_names:
                lines.append("%s:" % label_names[idx])
            comment = ""
            if op in (Op.LOAD_VAR, Op.STORE_VAR):
                comment = "  ; depth=%d slot=%d" % (operand >> 8,
                                                   operand & 0xFF)
            elif op == Op.MAKE_FUNCTION and operand < len(program.functions):
                comment = "  ; %s" % program.functions[operand].name
            elif op == Op.MAKE_VARIANT:
                comment = "  ; %s" % (["Some", "None", "Ok", "Err"]
                                      [operand])
            elif op == Op.FAIL:
                c = code.consts[operand] if operand < len(code.consts) \
                    else "?"
                comment = "  ; %r" % (c,)
            lines.append("  %-16s %-6d  ; @%d%s"
                         % (Op(op).name, operand, idx, comment))
    return "\n".join(lines) + "\n"


def assemble(text):
    """Parse assembly text (as produced by disassemble) into a Program."""
    functions = []
    code = None
    labels = {}   # label name -> instruction index (current function)
    jumps = []    # (instr_index, label_name) (current function)

    def finish_func():
        for instr_index, label_name in jumps:
            if label_name not in labels:
                raise CompileError("undefined label '%s'" % label_name)
            code.instrs[instr_index][1] = labels[label_name]

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # strip trailing comment (";" starts a comment outside strings)
        body = _strip_asm_comment(line)
        if not body:
            continue
        if body.startswith(".func "):
            if code is not None:
                finish_func()
            parts = body.split()
            name = parts[1]
            params = slots = 0
            for p in parts[2:]:
                if p.startswith("params="):
                    params = int(p[7:])
                elif p.startswith("slots="):
                    slots = int(p[6:])
            code = Code(name, ["#%d" % i for i in range(params)])
            code.nslots = slots
            functions.append(code)
            labels = {}
            jumps = []
            continue
        if code is None:
            raise CompileError("assembly outside .func: '%s'" % body)
        if body.startswith(".const "):
            rest = body[len(".const "):]
            _, _, cdef = rest.partition("=")
            code.consts.append(_parse_const(cdef))
            continue
        if body.endswith(":"):
            lname = body[:-1].strip()
            if not lname:
                raise CompileError("empty label")
            labels[lname] = len(code.instrs)
            continue
        parts = body.split()
        try:
            op = Op[parts[0]]
        except KeyError:
            raise CompileError("unknown opcode '%s'" % parts[0])
        operand = 0
        if len(parts) > 1:
            tok = parts[1]
            if (op in (Op.JUMP, Op.JUMP_IF_FALSE, Op.JUMP_IF_TRUE)
                    and not tok.lstrip("-").isdigit()):
                jumps.append((len(code.instrs), tok))
            else:
                try:
                    operand = int(tok)
                except ValueError:
                    raise CompileError("bad operand '%s'" % tok)
        code.instrs.append([int(op), operand, 0])
    if code is not None:
        finish_func()
    if not functions:
        raise CompileError("no .func blocks in assembly")
    return Program(functions, entry=0, source="<asm>")


def _strip_asm_comment(line):
    # `;` starts a comment unless inside a double-quoted string.
    in_str = False
    i = 0
    while i < len(line):
        c = line[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == ";":
                return line[:i].rstrip()
        i += 1
    return line.rstrip()


# ------------------------------------------------------------- entry API ----
def compile_source(source, filename="<source>", basedir="."):
    """Parse + type-check + compile Nova source to a Program.

    Raises CompileError on any lexer/parser/checker diagnostic or on
    use of language features outside the NovaVM subset.
    """
    _, diags = check_source(source, filename, basedir, is_entry=True)
    if diags:
        first = diags[0]
        raise CompileError(
            "check failed: [%s] %s" % (first.code, first.message),
            first.line, first.col)
    try:
        toks = Lexer(source, filename).tokenize()
    except LexError as e:
        raise CompileError("lexer: %s" % e, e.line, e.col)
    try:
        prog = Parser(toks, source, filename).parse_program()
    except ParseError as e:
        raise CompileError("syntax: %s" % e, e.line, e.col,
                           getattr(e, "hint", ""))
    except JsxError as e:
        raise CompileError("syntax (JSX): %s" % e, e.line, e.col)
    return Compiler(filename).compile_program(prog)


def compile_to_file(source_path, out_path):
    """Compile a .nova file to a .nvb bytecode file. Returns out_path."""
    with open(source_path) as fh:
        source = fh.read()
    basedir = os.path.dirname(os.path.abspath(source_path))
    program = compile_source(source, source_path, basedir)
    with open(out_path, "wb") as fh:
        fh.write(dump(program))
    return out_path
