# vm.py — NovaVM: a portable bytecode interpreter for the Nova subset.
#
# This is a simple Python dispatch loop over the bytecode defined in
# bytecode.py. It is the *portability* story ("write once, run anywhere":
# any host that can implement ~40 opcodes can run .nvb files), not a
# speed story — expect it to run slower than the tree-walking
# interpreter. See docs/v2-bytecode.md for the honest limits.
#
# Value model mirrors the interpreter: Ints are checked 64-bit (overflow
# is a runtime error, never silent wraparound), Floats are f64, Strings
# are UTF-8, Bools are strict, Lists are growable, Option/Result are the
# only absence/error values, and functions are closures over bytecode.

from .bytecode import Op, Program, Code, UNIT, load, CompileError

INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1

# Safety valve against infinite loops: the reference interpreter has no
# step limit, but a VM that hangs forever on `while true {}` is worse
# than one that says so. Generous enough to never trip on real programs.
EXEC_BUDGET = 100_000_000


class VMError(Exception):
    """A Nova runtime error inside the VM (carries the Nova error code)."""

    def __init__(self, code, message, line=0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line

    def __str__(self):
        if self.line:
            return "[%s] line %d: %s" % (self.code, self.line, self.message)
        return "[%s] %s" % (self.code, self.message)


# ---------------------------------------------------------------- values ----
class VUnit:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "()"


VUNIT = VUnit()


class VList:
    __slots__ = ("items",)

    def __init__(self, items):
        self.items = items


class VOption:
    __slots__ = ("value",)  # None means None

    def __init__(self, value):
        self.value = value


class VResult:
    __slots__ = ("is_ok", "value")

    def __init__(self, is_ok, value):
        self.is_ok = is_ok
        self.value = value


class VFunc:
    """A closure: bytecode plus the frame it was created in."""
    __slots__ = ("code", "frame")

    def __init__(self, code, frame):
        self.code = code
        self.frame = frame


class _Unset:
    def __repr__(self):
        return "<unset>"


UNSET = _Unset()


def nova_repr(v):
    # Must match the tree-walking interpreter's output exactly.
    if v is VUNIT:
        return "()"
    if v is UNSET:
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
    if isinstance(v, VFunc):
        return "<fn %s>" % v.code.name
    return str(v)


# ------------------------------------------------------------------ frames ----
class _FCode:
    """Decoded, execution-ready function."""
    __slots__ = ("name", "nparams", "nslots", "consts", "insns", "lines")

    def __init__(self, code):
        self.name = code.name
        self.nparams = len(code.param_names)
        self.nslots = code.nslots
        self.consts = [VUNIT if c is UNIT else c for c in code.consts]
        self.insns = [(op, arg) for op, arg, _ in code.instrs]
        self.lines = [ln for _, _, ln in code.instrs]


class _Frame:
    __slots__ = ("code", "cells", "parent", "ip", "stack_base")

    def __init__(self, code, cells, parent, stack_base):
        self.code = code
        self.cells = cells
        self.parent = parent
        self.ip = 0
        self.stack_base = stack_base


# ---------------------------------------------------------------------- VM ----
class VM:
    def __init__(self):
        self.output = []

    # -- errors ---------------------------------------------------------
    def _err(self, frame, code, message):
        line = 0
        if frame is not None:
            idx = (frame.ip - 3) // 3
            if 0 <= idx < len(frame.code.lines):
                line = frame.code.lines[idx]
        raise VMError(code, message, line)

    # -- checked integer arithmetic (mirrors the interpreter) -----------
    def _ck(self, frame, v, what):
        if v < INT_MIN or v > INT_MAX:
            self._err(frame, "R_INT_OVERFLOW",
                       "integer overflow in %s: Nova Ints are signed 64-bit "
                       "and never wrap silently" % what)
        return v

    def _int_divmod(self, frame, l, r, what):
        if r == 0:
            self._err(frame, "R_DIV_ZERO",
                       "integer %s by zero" % what)
        if l == INT_MIN and r == -1:
            self._err(frame, "R_INT_OVERFLOW", "INT_MIN / -1 overflows")
        q = abs(l) // abs(r)
        return -q if (l < 0) != (r < 0) else q

    def _equals(self, l, r):
        # Mirrors the tree-walking interpreter exactly — including its
        # quirk: Option/Result equality is *identity*, not structural.
        # (The interpreter's deep-compare branches sit inside a
        # `type(l) is not type(r)` guard that same-class operands never
        # enter, so `Some(1) == Some(1)` is False there too. `match` is
        # the intended way to compare variants.)
        if isinstance(l, bool) or isinstance(r, bool):
            return l is r
        if type(l) is not type(r):
            return False
        if isinstance(l, VList):
            return (len(l.items) == len(r.items)
                    and all(self._equals(a, b)
                            for a, b in zip(l.items, r.items)))
        return l == r

    # -- main loop --------------------------------------------------------
    def run(self, program):
        """Execute a compiled Program. Returns the list of printed lines."""
        fcodes = [_FCode(c) for c in program.functions]
        entry = fcodes[program.entry]
        stack = []
        frames = [_Frame(entry, [UNSET] * entry.nslots, None, 0)]
        steps = 0

        pop = stack.pop
        push = stack.append

        while frames:
            f = frames[-1]
            code = f.code
            insns = code.insns
            ip = f.ip
            op, arg = insns[ip]
            f.ip = ip + 1

            steps += 1
            if steps > EXEC_BUDGET:
                self._err(f, "R_BUDGET",
                           "execution budget exceeded (possible infinite "
                           "loop)")

            # -- stack --------------------------------------------------
            if op == Op.LOAD_CONST:
                push(code.consts[arg])
            elif op == Op.POP:
                pop()
            elif op == Op.DUP:
                push(stack[-1])

            # -- variables ----------------------------------------------
            elif op == Op.LOAD_VAR or op == Op.STORE_VAR:
                fr = f
                depth = arg >> 8
                slot = arg & 0xFF
                for _ in range(depth):
                    fr = fr.parent
                    if fr is None:
                        self._err(f, "E_INTERNAL", "bad variable scope")
                if op == Op.LOAD_VAR:
                    v = fr.cells[slot]
                    if v is UNSET:
                        self._err(f, "E_UNDEFINED",
                                   "variable used before initialization")
                    push(v)
                else:
                    fr.cells[slot] = pop()

            # -- arithmetic ----------------------------------------------
            elif op == Op.ADD:
                r = pop()
                l = pop()
                if (isinstance(l, int) and isinstance(r, int)
                        and not isinstance(l, bool)
                        and not isinstance(r, bool)):
                    push(self._ck(f, l + r, "addition"))
                elif isinstance(l, float) and isinstance(r, float):
                    push(l + r)
                elif isinstance(l, str) and isinstance(r, str):
                    push(l + r)
                else:
                    self._err(f, "E_TYPE_MISMATCH", "bad operands for +")
            elif op == Op.SUB or op == Op.MUL:
                r = pop()
                l = pop()
                what = "subtraction" if op == Op.SUB else "multiplication"
                if (isinstance(l, int) and isinstance(r, int)
                        and not isinstance(l, bool)
                        and not isinstance(r, bool)):
                    push(self._ck(f, l - r if op == Op.SUB else l * r,
                                  what))
                elif isinstance(l, float) and isinstance(r, float):
                    push(l - r if op == Op.SUB else l * r)
                else:
                    self._err(f, "E_TYPE_MISMATCH",
                               "bad operands for %s" % what)
            elif op == Op.DIV:
                r = pop()
                l = pop()
                if (isinstance(l, int) and isinstance(r, int)
                        and not isinstance(l, bool)
                        and not isinstance(r, bool)):
                    push(self._int_divmod(f, l, r, "division"))
                elif isinstance(l, float) and isinstance(r, float):
                    if r == 0:
                        self._err(f, "R_DIV_ZERO", "division by zero")
                    push(l / r)
                else:
                    self._err(f, "E_TYPE_MISMATCH", "bad operands for /")
            elif op == Op.MOD:
                r = pop()
                l = pop()
                if (isinstance(l, int) and isinstance(r, int)
                        and not isinstance(l, bool)
                        and not isinstance(r, bool)):
                    q = self._int_divmod(f, l, r, "modulo")
                    push(l - q * r)
                else:
                    self._err(f, "E_TYPE_MISMATCH",
                               "% on Floats not supported"
                               if isinstance(l, float) else
                               "bad operands for %")
            elif op == Op.NEG:
                v = pop()
                if isinstance(v, bool):
                    self._err(f, "E_TYPE_MISMATCH", "unary - on Bool")
                elif isinstance(v, int):
                    push(self._ck(f, -v, "negation"))
                elif isinstance(v, float):
                    push(-v)
                else:
                    self._err(f, "E_TYPE_MISMATCH",
                               "unary - needs Int or Float")
            elif op == Op.NOT:
                v = pop()
                if v is True:
                    push(False)
                elif v is False:
                    push(True)
                else:
                    self._err(f, "E_TYPE_MISMATCH", "not needs a Bool")

            # -- comparison -----------------------------------------------
            elif op == Op.EQ or op == Op.NE:
                r = pop()
                l = pop()
                eq = self._equals(l, r)
                push(eq if op == Op.EQ else not eq)
            elif op in (Op.LT, Op.LE, Op.GT, Op.GE):
                r = pop()
                l = pop()
                if (isinstance(l, bool) or isinstance(r, bool)
                        or type(l) is not type(r)
                        or not isinstance(l, (int, float))):
                    self._err(f, "E_TYPE_MISMATCH",
                               "comparison needs two Ints or two Floats")
                push({Op.LT: l < r, Op.LE: l <= r,
                      Op.GT: l > r, Op.GE: l >= r}[op])

            # -- jumps ------------------------------------------------------
            elif op == Op.JUMP:
                f.ip = arg
            elif op == Op.JUMP_IF_FALSE:
                v = pop()
                if v is False:
                    f.ip = arg
                elif v is not True:
                    self._err(f, "E_TYPE_MISMATCH",
                               "condition must be Bool")
            elif op == Op.JUMP_IF_TRUE:
                v = pop()
                if v is True:
                    f.ip = arg
                elif v is not False:
                    self._err(f, "E_TYPE_MISMATCH",
                               "condition must be Bool")

            # -- functions ----------------------------------------------------
            elif op == Op.MAKE_FUNCTION:
                push(VFunc(fcodes[arg], f))
            elif op == Op.CALL:
                # Convention: args are pushed first, callee last, so the
                # callee is popped first.
                callee = pop()
                args = [pop() for _ in range(arg)]
                args.reverse()
                if not isinstance(callee, VFunc):
                    self._err(f, "E_NOT_CALLABLE", "not callable")
                ccode = callee.code
                if ccode.nparams != arg:
                    self._err(f, "E_ARGC",
                               "'%s' takes %d argument(s), got %d"
                               % (ccode.name, ccode.nparams, arg))
                cells = [UNSET] * ccode.nslots
                for i, v in enumerate(args):
                    cells[i] = v
                frames.append(_Frame(ccode, cells, callee.frame,
                                     len(stack)))
            elif op == Op.RETURN:
                v = pop()
                base = f.stack_base
                del stack[base:]
                frames.pop()
                if frames:
                    push(v)
                # else: entry returned — halt

            # -- data structures ----------------------------------------------
            elif op == Op.BUILD_LIST:
                items = [pop() for _ in range(arg)]
                items.reverse()
                push(VList(items))
            elif op == Op.MAKE_VARIANT:
                if arg == 1:
                    push(VOption(None))
                else:
                    v = pop()
                    if arg == 0:
                        push(VOption(v))
                    elif arg == 2:
                        push(VResult(True, v))
                    elif arg == 3:
                        push(VResult(False, v))
                    else:
                        self._err(f, "E_INTERNAL", "bad variant kind")
            elif op == Op.INDEX:
                i = pop()
                o = pop()
                if (isinstance(o, VList) and isinstance(i, int)
                        and not isinstance(i, bool)):
                    if not 0 <= i < len(o.items):
                        self._err(f, "R_INDEX_OUT_OF_BOUNDS",
                                   "index %d" % i)
                    push(o.items[i])
                else:
                    self._err(f, "E_TYPE_MISMATCH", "cannot index")
            elif op == Op.STORE_INDEX:
                v = pop()
                i = pop()
                o = pop()
                if (isinstance(o, VList) and isinstance(i, int)
                        and not isinstance(i, bool)
                        and 0 <= i < len(o.items)):
                    o.items[i] = v
                    push(v)
                elif isinstance(o, VList):
                    self._err(f, "R_INDEX_OUT_OF_BOUNDS", "index %d" % i
                               if isinstance(i, int) else "bad index")
                else:
                    self._err(f, "E_TYPE_MISMATCH", "cannot index-assign")
            elif op == Op.TAG_OF:
                v = pop()
                if isinstance(v, VOption):
                    push(0 if v.value is None else 1)
                elif isinstance(v, VResult):
                    push(2 if v.is_ok else 3)
                else:
                    push(4)
            elif op == Op.VARIANT_VALUE:
                v = pop()
                if isinstance(v, VOption) and v.value is not None:
                    push(v.value)
                elif isinstance(v, VResult):
                    push(v.value)
                else:
                    self._err(f, "E_TYPE_MISMATCH",
                               "not a Some/Ok/Err value")
            elif op == Op.UNWRAP_OR_RETURN:
                # The `?` operator.
                v = pop()
                # Propagating an Err/None returns it as this call's
                # result — the same frame-unwind as RETURN.
                propagate = (
                    (isinstance(v, VResult) and not v.is_ok)
                    or (isinstance(v, VOption) and v.value is None))
                if isinstance(v, VResult) and v.is_ok:
                    push(v.value)
                elif isinstance(v, VOption) and v.value is not None:
                    push(v.value)
                elif propagate:
                    base = f.stack_base
                    del stack[base:]
                    frames.pop()
                    if frames:
                        push(v)
                    else:
                        self._err(None, "E_QUESTION",
                                   "`?` propagated out of the entry point")
                else:
                    self._err(f, "E_QUESTION", "? needs Result or Option")

            # -- builtins -------------------------------------------------------
            elif op == Op.LEN:
                v = pop()
                if isinstance(v, VList):
                    push(len(v.items))
                else:
                    self._err(f, "E_TYPE_MISMATCH", "len() needs a List")
            elif op == Op.LIST_PUSH:
                v = pop()
                o = pop()
                if not isinstance(o, VList):
                    self._err(f, "E_TYPE_MISMATCH",
                               "push needs a List")
                o.items.append(v)
                push(VUNIT)
            elif op == Op.UNWRAP_MSG:
                msg = pop()
                v = pop()
                text = msg if isinstance(msg, str) else None
                if isinstance(v, VOption):
                    if v.value is None:
                        self._err(f, "R_UNWRAP_NONE",
                                   "unwrap on None%s"
                                   % (": " + text if text else ""))
                    push(v.value)
                elif isinstance(v, VResult):
                    if not v.is_ok:
                        self._err(f, "R_UNWRAP_ERR",
                                   "unwrap on Err%s"
                                   % (": " + text if text else ""))
                    push(v.value)
                else:
                    self._err(f, "E_TYPE_MISMATCH",
                               "unwrap needs Option or Result")
            elif op == Op.PRINT:
                vals = [pop() for _ in range(arg)]
                vals.reverse()
                self.output.append(" ".join(nova_repr(v) for v in vals))
                push(VUNIT)
            elif op == Op.STR:
                push(nova_repr(pop()))
            elif op == Op.INT_OF:
                v = pop()
                if isinstance(v, bool):
                    self._err(f, "E_TYPE_MISMATCH",
                               "int() needs an Int or Float")
                elif isinstance(v, int):
                    t = v
                elif isinstance(v, float):
                    import math
                    t = math.trunc(v)
                else:
                    self._err(f, "E_TYPE_MISMATCH",
                               "int() needs an Int or Float")
                if t < INT_MIN or t > INT_MAX:
                    self._err(f, "R_INT_OVERFLOW", "int() out of range")
                push(t)
            elif op == Op.ASSERT:
                vals = [pop() for _ in range(arg)]
                vals.reverse()
                if vals[0] is not True:
                    msg = vals[1] if arg == 2 else "assertion failed"
                    self._err(f, "R_ASSERT", "assert failed: %s" % msg)
                push(VUNIT)
            elif op == Op.FAIL:
                self._err(f, "E_INTERNAL", str(code.consts[arg]))
            else:
                self._err(f, "E_INTERNAL",
                           "unknown opcode %d" % op)
        return self.output


def run_bytecode(path):
    """Load a .nvb file and execute it. Returns the printed output lines.

    Raises CompileError if the file is not valid NovaVM bytecode, and
    VMError on any Nova runtime error.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        program = load(data)
    except CompileError:
        raise
    except Exception as e:
        raise CompileError("cannot read bytecode file: %s" % e)
    return VM().run(program)
