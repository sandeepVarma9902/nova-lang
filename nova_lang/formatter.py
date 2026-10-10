# formatter.py — Nova canonical formatter.
#
# `nova fmt` parses a file and reprints it from the AST, so formatting is
# canonical by construction: there is exactly one way a Nova program looks.

from . import ast as A


class Formatter:
    def __init__(self):
        self.parts = []
        self.indent = 0

    def out(self):
        return "".join(self.parts)

    def w(self, s):
        self.parts.append(s)

    def nl(self):
        self.parts.append("\n" + "  " * self.indent)

    def fmt_program(self, prog):
        for i, s in enumerate(prog.stmts):
            if i:
                self.nl()
            self.fmt_top(s)
        self.w("\n")
        return self.out()

    def fmt_top(self, node):
        m = "_f_" + type(node).__name__
        getattr(self, m)(node)

    # -- declarations -------------------------------------------------
    def _f_Import(self, n):
        self.w("import %s" % n.name.replace("/", "."))

    def _f_LetDecl(self, n):
        self.w("let %s" % n.name)
        if n.ann:
            self.w(": ")
            self.fmt_type(n.ann)
        self.w(" = ")
        self.fmt_expr(n.expr)

    def _f_VarDecl(self, n):
        self.w("var %s" % n.name)
        if n.ann:
            self.w(": ")
            self.fmt_type(n.ann)
        self.w(" = ")
        self.fmt_expr(n.expr)

    def _f_StreamDecl(self, n):
        self.w("stream %s: " % n.name)
        self.fmt_type(n.type)
        if n.topic:
            self.w(' = "%s"' % n.topic)

    def _f_ContextDecl(self, n):
        self.w("context %s: " % n.name)
        self.fmt_type(n.type)
        if n.default is not None:
            self.w(" = ")
            self.fmt_expr(n.default)

    def fmt_fn_sig(self, n, kind):
        eff = n.effect
        if eff.startswith("async"):
            self.w("async ")
            eff = eff[len("async"):].strip()
        if eff == "effect":
            self.w("effect ")
        elif eff and eff != "pure":
            self.w("effect %s " % eff)
        self.w("%s %s(" % (kind, n.name))
        self.w(", ".join("%s: %s" % (p.name, self.str_type(p.type))
                         for p in n.params))
        self.w(")")
        if n.ret:
            self.w(" -> ")
            self.fmt_type(n.ret)

    def _f_FnDecl(self, n):
        self.fmt_fn_sig(n, "fn")
        self.fmt_contracts(n)
        self.w(" ")
        self.fmt_block(n.body)

    def _f_AiDecl(self, n):
        self.w("ai fn %s(" % n.name)
        self.w(", ".join("%s: %s" % (p.name, self.str_type(p.type))
                         for p in n.params))
        self.w(") -> ")
        self.fmt_type(n.ret)
        for k, v in n.config.items():
            self.w("\n  %s = %s" % (k, self.lit(v)))
        self.w(" ")
        self.fmt_block(n.body)

    def lit(self, v):
        if isinstance(v, str):
            return '"%s"' % v
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    def fmt_contracts(self, n):
        for r in n.requires:
            self.w("\n  requires ")
            self.fmt_expr(r)
        for e in n.ensures:
            self.w("\n  ensures ")
            self.fmt_expr(e)

    def _f_ClassDecl(self, n):
        if n.is_data:
            self.w("data class %s(" % n.name)
            self.w(", ".join("%s: %s" % (m.name, self.str_type(m.type))
                             for m in n.members))
            self.w(")")
            return
        self.w("class %s" % n.name)
        if n.parent:
            self.w(" extends %s" % n.parent)
        if n.interfaces:
            self.w(" implements %s" % ", ".join(n.interfaces))
        self.w(" {")
        self.indent += 1
        for m in n.members:
            self.nl()
            self.fmt_member(m)
        self.indent -= 1
        self.nl()
        self.w("}")

    def fmt_member(self, m):
        if isinstance(m, A.FieldDecl):
            self.w(("var " if m.mutable else "let ") + m.name + ": ")
            self.fmt_type(m.type)
        elif isinstance(m, A.InitDecl):
            self.w("init(")
            self.w(", ".join("%s: %s" % (p.name, self.str_type(p.type))
                             for p in m.params))
            self.w(") ")
            self.fmt_block(m.body)
        elif isinstance(m, A.MethodDecl):
            if m.is_override:
                self.w("override ")
            self.fmt_fn_sig(m, "fn")
            self.fmt_contracts(m)
            self.w(" ")
            self.fmt_block(m.body)

    def _f_InterfaceDecl(self, n):
        self.w("interface %s {" % n.name)
        self.indent += 1
        for m in n.methods:
            self.nl()
            self.w("fn %s(" % m.name)
            self.w(", ".join("%s: %s" % (p.name, self.str_type(p.type))
                             for p in m.params))
            self.w(")")
            if m.ret:
                self.w(" -> ")
                self.fmt_type(m.ret)
        self.indent -= 1
        self.nl()
        self.w("}")

    def _f_ComponentDecl(self, n):
        self.w("component %s(" % n.name)
        self.w(", ".join("%s: %s" % (p.name, self.str_type(p.type))
                         for p in n.params))
        self.w(") {")
        self.indent += 1
        for s in n.states:
            self.nl()
            self.w("state %s: " % s.name)
            self.fmt_type(s.type)
            self.w(" = ")
            self.fmt_expr(s.init)
        self.nl()
        self.w("view {")
        self.indent += 1
        for c in n.view:
            self.nl()
            self.fmt_jsx(c)
        self.indent -= 1
        self.nl()
        self.w("}")
        self.indent -= 1
        self.nl()
        self.w("}")

    # -- statements ---------------------------------------------------
    def fmt_block(self, b):
        self.w("{")
        self.indent += 1
        for s in b.stmts:
            self.nl()
            self.fmt_stmt(s)
        self.indent -= 1
        self.nl()
        self.w("}")

    def fmt_stmt(self, node):
        m = "_f_" + type(node).__name__
        fn = getattr(self, m, None)
        if fn:
            fn(node)
        else:
            self.fmt_expr(node)

    def _f_ExprStmt(self, n):
        self.fmt_expr(n.expr)

    def _f_Return(self, n):
        self.w("return")
        if n.expr is not None:
            self.w(" ")
            self.fmt_expr(n.expr)

    def _f_IfStmt(self, n):
        self.w("if ")
        self.fmt_expr(n.cond)
        self.w(" ")
        self.fmt_block(n.then_b)
        if n.else_b is not None:
            self.w(" else ")
            if isinstance(n.else_b, A.IfStmt):
                self.fmt_stmt(n.else_b)
            else:
                self.fmt_block(n.else_b)

    def _f_WhileStmt(self, n):
        self.w("while ")
        self.fmt_expr(n.cond)
        self.w(" ")
        self.fmt_block(n.body)

    def _f_ForStmt(self, n):
        self.w("for %s in " % n.var)
        self.fmt_expr(n.iter)
        self.w(" ")
        self.fmt_block(n.body)

    def _f_SuperCall(self, n):
        self.w("super(")
        self.fmt_args(n.args)
        self.w(")")

    def _f_Block(self, b):
        self.fmt_block(b)

    # -- expressions --------------------------------------------------
    def fmt_expr(self, node):
        m = "_fe_" + type(node).__name__
        getattr(self, m)(node)

    def _fe_IntLit(self, n):
        self.w(str(n.value))

    def _fe_FloatLit(self, n):
        self.w(repr(n.value))

    def _fe_StrLit(self, n):
        self.w('"%s"' % n.value.replace("\\", "\\\\").replace('"', '\\"'))

    def _fe_BoolLit(self, n):
        self.w("true" if n.value else "false")

    def _fe_NoneLit(self, n):
        self.w("None")

    def _fe_Spawn(self, n):
        self.w("spawn ")
        self.fmt_block(n.block)

    def _fe_SpawnAsync(self, n):
        self.w("spawn_async ")
        self.fmt_block(n.block)

    def _fe_Nursery(self, n):
        self.w("nursery ")
        self.fmt_block(n.block)

    def _fe_Provide(self, n):
        self.w("provide %s = " % n.name)
        self.fmt_expr(n.value)
        self.w(" ")
        self.fmt_block(n.block)

    def _fe_Consume(self, n):
        self.w("consume %s" % n.name)

    def _fe_Var(self, n):
        self.w(n.name)

    def _fe_This(self, n):
        self.w("this")

    def _fe_Super(self, n):
        self.w("super")

    def _fe_ResultVar(self, n):
        self.w("result")

    def _fe_SomeExpr(self, n):
        self.w("Some(")
        self.fmt_expr(n.expr)
        self.w(")")

    def _fe_OkExpr(self, n):
        self.w("Ok(")
        self.fmt_expr(n.expr)
        self.w(")")

    def _fe_ErrExpr(self, n):
        self.w("Err(")
        self.fmt_expr(n.expr)
        self.w(")")

    def _fe_ListLit(self, n):
        self.w("[")
        for i, e in enumerate(n.elems):
            if i:
                self.w(", ")
            self.fmt_expr(e)
        self.w("]")

    def _fe_Call(self, n):
        self.fmt_expr(n.func)
        self.w("(")
        self.fmt_args(n.args)
        self.w(")")

    def fmt_args(self, args):
        for i, (name, e) in enumerate(args):
            if i:
                self.w(", ")
            if name:
                self.w("%s: " % name)
            self.fmt_expr(e)

    def _fe_MethodCall(self, n):
        self.fmt_expr(n.obj)
        self.w(".%s(" % n.name)
        self.fmt_args(n.args)
        self.w(")")

    def _fe_Field(self, n):
        self.fmt_expr(n.obj)
        self.w(".%s" % n.name)

    def _fe_Index(self, n):
        self.fmt_expr(n.obj)
        self.w("[")
        self.fmt_expr(n.index)
        self.w("]")

    _PREC = {"or": 1, "and": 2, "==": 3, "!=": 3, "<": 4, "<=": 4, ">": 4,
             ">=": 4, "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}

    def _fe_Binary(self, n):
        p = self._PREC[n.op]
        lp = self._PREC.get(getattr(n.left, "op", None), 99)
        rp = self._PREC.get(getattr(n.right, "op", None), 99)
        if lp < p:
            self.w("(")
            self.fmt_expr(n.left)
            self.w(")")
        else:
            self.fmt_expr(n.left)
        self.w(" %s " % n.op)
        if rp <= p:
            self.w("(")
            self.fmt_expr(n.right)
            self.w(")")
        else:
            self.fmt_expr(n.right)

    def _fe_Unary(self, n):
        if n.op == "not":
            self.w("not ")
        else:
            self.w("-")
        self.fmt_expr(n.expr)

    def _fe_Assign(self, n):
        self.fmt_expr(n.target)
        self.w(" = ")
        self.fmt_expr(n.value)

    def _fe_Question(self, n):
        self.fmt_expr(n.expr)
        self.w("?")

    def _fe_IfExpr(self, n):
        self.w("if ")
        self.fmt_expr(n.cond)
        self.w(" ")
        self.fmt_block(n.then_b)
        self.w(" else ")
        self.fmt_block(n.else_b)

    def _fe_Match(self, n):
        self.w("match ")
        self.fmt_expr(n.scrutinee)
        self.w(" {")
        self.indent += 1
        for arm in n.arms:
            self.nl()
            self.fmt_pattern(arm.pattern)
            self.w(" => ")
            self.fmt_expr(arm.expr)
        self.indent -= 1
        self.nl()
        self.w("}")

    def fmt_pattern(self, p):
        if isinstance(p, A.PatSome):
            self.w("Some(%s)" % p.name)
        elif isinstance(p, A.PatNone):
            self.w("None")
        elif isinstance(p, A.PatOk):
            self.w("Ok(%s)" % p.name)
        elif isinstance(p, A.PatErr):
            self.w("Err(%s)" % p.name)
        elif isinstance(p, A.PatLit):
            self.w(self.lit(p.value))
        elif isinstance(p, A.PatWild):
            self.w("_")
        elif isinstance(p, A.PatVar):
            self.w(p.name)

    # -- types --------------------------------------------------------
    def fmt_type(self, t):
        self.w(self.str_type(t))

    def str_type(self, t):
        if t.args:
            parts = []
            for a in t.args:
                if isinstance(a, A.ConstTypeArg):
                    parts.append(str(a.value))
                else:
                    parts.append(self.str_type(a))
            return "%s[%s]" % (t.name, ", ".join(parts))
        return t.name

    # -- JSX ----------------------------------------------------------
    def fmt_jsx(self, node):
        if isinstance(node, A.JText):
            self.w(node.text)
        elif isinstance(node, A.JExpr):
            self.w("{")
            self.fmt_expr(node.expr)
            self.w("}")
        elif isinstance(node, A.JFor):
            self.w("<for %s in {" % node.var)
            self.fmt_expr(node.iter)
            self.w("}>")
            self.indent += 1
            for c in node.children:
                self.nl()
                self.fmt_jsx(c)
            self.indent -= 1
            self.nl()
            self.w("</for>")
        elif isinstance(node, A.JElement):
            self.w("<%s" % node.tag)
            for attr in node.attrs:
                self.w(" %s" % attr.name)
                if attr.value is not None:
                    if isinstance(attr.value, A.StrLit):
                        self.w('="%s"' % attr.value.value)
                    elif isinstance(attr.value, A.BoolLit):
                        pass
                    else:
                        self.w("={")
                        self.fmt_expr(attr.value)
                        self.w("}")
            if not node.children:
                self.w(" />")
                return
            self.w(">")
            self.indent += 1
            for c in node.children:
                self.nl()
                self.fmt_jsx(c)
            self.indent -= 1
            self.nl()
            self.w("</%s>" % node.tag)


def format_source(source, filename="<input>"):
    from .lexer import Lexer
    from .parser import Parser
    toks = Lexer(source, filename).tokenize()
    prog = Parser(toks, source, filename).parse_program()
    return Formatter().fmt_program(prog)
