# parser.py — Nova recursive-descent parser.
#
# Newlines separate statements (except inside () and [], handled by the
# lexer). JSX view blocks are scanned at the character level: the parser
# extracts the raw source span of a `view { ... }` block and parses it with
# a dedicated mini-parser, so the main token stream never needs an XML mode.

from .lexer import Lexer, LexError
from . import ast as A


class ParseError(Exception):
    def __init__(self, message, line, col, hint=""):
        super().__init__(message)
        self.line = line
        self.col = col
        self.hint = hint


class Parser:
    def __init__(self, tokens, source, filename="<input>"):
        self.toks = tokens
        self.source = source
        self.filename = filename
        self.pos = 0
        # char offsets of each line start (1-based lines)
        self.line_starts = [0]
        for i, ch in enumerate(source):
            if ch == "\n":
                self.line_starts.append(i + 1)

    # -- token helpers ------------------------------------------------
    def peek(self, i=0):
        j = self.pos + i
        if j < len(self.toks):
            return self.toks[j]
        return self.toks[-1]

    def next(self):
        t = self.peek()
        if t.kind != "EOF":
            self.pos += 1
        return t

    def at(self, kind, i=0):
        return self.peek(i).kind == kind

    def accept(self, kind):
        if self.at(kind):
            return self.next()
        return None

    def expect(self, kind, what=None):
        t = self.peek()
        if t.kind != kind:
            raise ParseError(
                "expected %s, found %s" % (what or kind, t.kind),
                t.line, t.col)
        return self.next()

    def skip_newlines(self):
        while self.at("NEWLINE"):
            self.next()

    def offset_of(self, line, col):
        return self.line_starts[line - 1] + (col - 1)

    # -- entry --------------------------------------------------------
    def parse_program(self):
        stmts = []
        self.skip_newlines()
        while not self.at("EOF"):
            stmts.append(self.parse_top_level())
            self.skip_newlines()
        return A.Program(stmts=stmts)

    def parse_top_level(self):
        t = self.peek()
        if t.kind == "import":
            return self.parse_import()
        return self.parse_decl_or_stmt()

    def parse_import(self):
        # Dotted imports allowed: `import std.web` resolves
        # std/web.nova (local dir first, then the stdlib).
        t = self.expect("import")
        parts = [self.expect("IDENT", "module name").value]
        while self.accept("."):
            parts.append(self.expect("IDENT", "module name").value)
        return A.Import(name="/".join(parts), line=t.line, col=t.col)

    # -- declarations -------------------------------------------------
    def parse_decl_or_stmt(self):
        t = self.peek()
        if t.kind in ("let", "var"):
            return self.parse_let_var()
        if t.kind == "fn" or t.kind == "effect":
            return self.parse_fn()
        if t.kind == "async":
            return self.parse_async_fn()
        if t.kind == "stream":
            return self.parse_stream_decl()
        if t.kind == "context":
            return self.parse_context_decl()
        if t.kind == "ai":
            return self.parse_ai()
        if t.kind == "class" or (t.kind == "data" and self.peek(1).kind == "class"):
            return self.parse_class()
        if t.kind == "interface":
            return self.parse_interface()
        if t.kind == "component":
            return self.parse_component()
        return self.parse_stmt()

    def parse_let_var(self):
        t = self.next()  # let | var
        name = self.expect("IDENT", "variable name")
        ann = None
        if self.accept(":"):
            ann = self.parse_type()
        self.expect("=", "'='")
        expr = self.parse_expr()
        cls = A.LetDecl if t.kind == "let" else A.VarDecl
        return cls(name=name.value, ann=ann, expr=expr, line=t.line, col=t.col)

    def parse_effect_kind(self):
        # "effect" ["io" | "net"]  ->  "effect" | "io" | "net"
        self.expect("effect")
        if self.peek().kind == "IDENT" and self.peek().value in ("io", "net"):
            return self.next().value
        return "effect"

    def parse_fn(self):
        effect = "pure"
        if self.at("effect"):
            effect = self.parse_effect_kind()
        t = self.expect("fn")
        name = self.expect("IDENT", "function name")
        params = self.parse_params()
        ret = None
        if self.accept("->"):
            ret = self.parse_type()
        requires, ensures = self.parse_contracts()
        body = self.parse_block()
        return A.FnDecl(name=name.value, params=params, ret=ret,
                        requires=requires, ensures=ensures, body=body,
                        effect=effect, line=t.line, col=t.col)

    def parse_async_fn(self):
        # async [effect [io | net]] fn — async may compose with io/net so
        # offloaded (spawn-wrapped) IO is expressible from async code.
        t = self.expect("async")
        node = self.parse_fn()
        if node.effect in ("pure", "effect"):
            node.effect = "async"
        elif node.effect in ("io", "net"):
            node.effect = "async " + node.effect
        else:
            raise ParseError("async cannot combine with '%s'" % node.effect,
                             t.line, t.col)
        return node

    def parse_ai(self):
        t = self.expect("ai")
        self.expect("fn")
        name = self.expect("IDENT", "ai function name")
        params = self.parse_params()
        self.expect("->", "'-> return type (the output schema)'")
        ret = self.parse_type()
        config = {}
        while True:
            self.skip_newlines()
            if not (self.peek().kind == "IDENT"
                    and self.peek(1).kind == "="):
                break
            key = self.next().value
            self.next()  # =
            vt = self.next()
            if vt.kind not in ("STRING", "INT", "FLOAT", "true", "false"):
                raise ParseError("ai config value must be a literal",
                                 vt.line, vt.col)
            val = vt.value
            if vt.kind == "true":
                val = True
            elif vt.kind == "false":
                val = False
            config[key] = val
        body = self.parse_block()
        return A.AiDecl(name=name.value, params=params, ret=ret,
                        config=config, body=body, line=t.line, col=t.col)

    def parse_contracts(self):
        requires, ensures = [], []
        while True:
            self.skip_newlines()
            if self.at("requires"):
                self.next()
                requires.append(self.parse_expr())
            elif self.at("ensures"):
                self.next()
                ensures.append(self.parse_expr())
            else:
                break
        return requires, ensures

    def parse_params(self):
        self.expect("(", "'('")
        params = []
        self.skip_newlines()
        while not self.at(")"):
            nt = self.expect("IDENT", "parameter name")
            self.expect(":", "':'")
            ty = self.parse_type()
            params.append(A.Param(name=nt.value, type=ty,
                                  line=nt.line, col=nt.col))
            if not self.accept(","):
                break
            self.skip_newlines()
        self.expect(")", "')'")
        return params

    def parse_type(self):
        t = self.expect("IDENT", "type name")
        args = []
        if self.accept("["):
            args.append(self.parse_type_arg())
            while self.accept(","):
                args.append(self.parse_type_arg())
            self.expect("]", "']'")
        return A.TypeNode(name=t.value, args=args, line=t.line, col=t.col)

    def parse_type_arg(self):
        # Type arguments are types, except Vector[N] which takes a
        # constant integer dimension.
        if self.peek().kind == "INT":
            t = self.next()
            return A.ConstTypeArg(value=t.value, line=t.line, col=t.col)
        return self.parse_type()

    def parse_class(self):
        is_data = False
        if self.at("data"):
            self.next()
            is_data = True
        t = self.expect("class")
        name = self.expect("IDENT", "class name")
        parent, interfaces = None, []
        if self.at("extends"):
            self.next()
            parent = self.expect("IDENT", "parent class").value
        if self.at("implements"):
            self.next()
            interfaces.append(self.expect("IDENT", "interface").value)
            while self.accept(","):
                interfaces.append(self.expect("IDENT", "interface").value)
        if is_data:
            # data class Point(x: Int, y: Int) — fields come from the header
            params = self.parse_params()
            members = [A.FieldDecl(name=p.name, type=p.type, mutable=False,
                                   line=p.line, col=p.col) for p in params]
            return A.ClassDecl(name=name.value, parent=parent,
                               interfaces=interfaces, members=members,
                               is_data=True, line=t.line, col=t.col)
        self.expect("{", "'{'")
        members = []
        self.skip_newlines()
        while not self.at("}"):
            members.append(self.parse_member())
            self.skip_newlines()
        self.expect("}", "'}'")
        return A.ClassDecl(name=name.value, parent=parent,
                           interfaces=interfaces, members=members,
                           is_data=False, line=t.line, col=t.col)

    def parse_member(self):
        t = self.peek()
        if t.kind in ("let", "var"):
            nt = self.next()
            name = self.expect("IDENT", "field name")
            self.expect(":", "':'")
            ty = self.parse_type()
            return A.FieldDecl(name=name.value, type=ty,
                               mutable=(nt.kind == "var"),
                               line=nt.line, col=nt.col)
        if t.kind == "init":
            self.next()
            params = self.parse_params()
            body = self.parse_block()
            return A.InitDecl(params=params, body=body, line=t.line, col=t.col)
        # method: ["async"] ["override"] ["effect" ["io"|"net"]] "fn" ...
        is_override = False
        if t.kind == "override":
            self.next()
            is_override = True
        is_async = False
        if self.peek().kind == "async":
            self.next()
            is_async = True
        effect = "pure"
        if self.at("effect"):
            effect = self.parse_effect_kind()
        if is_async:
            if effect in ("pure", "effect"):
                effect = "async"
            else:
                effect = "async " + effect
        ft = self.expect("fn")
        name = self.expect("IDENT", "method name")
        params = self.parse_params()
        ret = None
        if self.accept("->"):
            ret = self.parse_type()
        requires, ensures = self.parse_contracts()
        body = self.parse_block()
        return A.MethodDecl(name=name.value, params=params, ret=ret,
                            requires=requires, ensures=ensures, body=body,
                            effect=effect, is_override=is_override,
                            line=ft.line, col=ft.col)

    def parse_interface(self):
        t = self.expect("interface")
        name = self.expect("IDENT", "interface name")
        self.expect("{", "'{'")
        methods = []
        self.skip_newlines()
        while not self.at("}"):
            ft = self.expect("fn")
            mname = self.expect("IDENT", "method name")
            params = self.parse_params()
            ret = None
            if self.accept("->"):
                ret = self.parse_type()
            methods.append(A.MethodDecl(name=mname.value, params=params,
                                        ret=ret, line=ft.line, col=ft.col))
            self.skip_newlines()
        self.expect("}", "'}'")
        return A.InterfaceDecl(name=name.value, methods=methods,
                               line=t.line, col=t.col)

    def parse_component(self):
        t = self.expect("component")
        name = self.expect("IDENT", "component name")
        params = self.parse_params()
        self.expect("{", "'{'")
        states = []
        self.skip_newlines()
        while self.at("state"):
            st = self.next()
            sname = self.expect("IDENT", "state name")
            self.expect(":", "':'")
            ty = self.parse_type()
            self.expect("=", "'='")
            init = self.parse_expr()
            states.append(A.StateDecl(name=sname.value, type=ty, init=init,
                                      line=st.line, col=st.col))
            self.skip_newlines()
        self.expect("view", "'view'")
        # Extract the raw source span of the view block and parse JSX from it.
        brace = self.expect("{", "'{'")
        start_off = self.offset_of(brace.line, brace.col)
        inner, close_off = self.match_braces(start_off)
        base_line, base_col = brace.line, brace.col + 1
        view = JsxParser(inner, self.filename,
                         base_line, base_col).parse_children_top()
        # Skip main-stream tokens covered by the span, then consume its '}'.
        while self.pos < len(self.toks):
            tk = self.peek()
            if tk.kind == "EOF" or self.offset_of(tk.line, tk.col) >= close_off:
                break
            self.next()
        self.expect("}", "'}'")  # close of view
        self.skip_newlines()
        self.expect("}", "'}'")  # close of component
        return A.ComponentDecl(name=name.value, params=params, states=states,
                               view=view, line=t.line, col=t.col)

    def match_braces(self, open_off):
        """Given offset of '{', return (inner_text, offset_of_matching_'}')."""
        depth = 0
        i = open_off
        in_str = False
        while i < len(self.source):
            ch = self.source[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return self.source[open_off + 1:i], i
            i += 1
        t = self.peek()
        raise ParseError("unterminated view block", t.line, t.col)

    # -- statements ---------------------------------------------------
    def parse_block(self):
        t = self.expect("{", "'{'")
        stmts = []
        self.skip_newlines()
        while not self.at("}"):
            stmts.append(self.parse_decl_or_stmt())
            self.skip_newlines()
        self.expect("}", "'}'")
        return A.Block(stmts=stmts, line=t.line, col=t.col)

    def parse_stmt(self):
        t = self.peek()
        if t.kind == "return":
            self.next()
            expr = None
            if not self.at("NEWLINE") and not self.at("}"):
                expr = self.parse_expr()
            return A.Return(expr=expr, line=t.line, col=t.col)
        if t.kind == "if" and self.peek(1).kind != "(":
            # statement-if: `if cond {` — expression-if handled in parse_primary
            # Disambiguate: statement position always parses as statement here.
            return self.parse_if_stmt()
        if t.kind == "while":
            self.next()
            cond = self.parse_expr()
            body = self.parse_block()
            return A.WhileStmt(cond=cond, body=body, line=t.line, col=t.col)
        if t.kind == "for":
            self.next()
            var = self.expect("IDENT", "loop variable").value
            self.expect("in", "'in'")
            it = self.parse_expr()
            body = self.parse_block()
            return A.ForStmt(var=var, iter=it, body=body, line=t.line, col=t.col)
        if t.kind == "super" and self.peek(1).kind == "(":
            self.next()
            args = self.parse_args()
            return A.SuperCall(args=args, line=t.line, col=t.col)
        expr = self.parse_expr()
        return A.ExprStmt(expr=expr, line=expr.line, col=expr.col)

    def parse_if_stmt(self):
        t = self.expect("if")
        cond = self.parse_expr()
        then_b = self.parse_block()
        else_b = None
        self.skip_newlines()
        if self.at("else"):
            self.next()
            self.skip_newlines()
            if self.at("if"):
                else_b = self.parse_if_stmt()
            else:
                else_b = self.parse_block()
        return A.IfStmt(cond=cond, then_b=then_b, else_b=else_b,
                        line=t.line, col=t.col)

    # -- expressions --------------------------------------------------
    def parse_expr(self):
        return self.parse_assign()

    def parse_assign(self):
        left = self.parse_or()
        if self.at("="):
            t = self.next()
            right = self.parse_assign()
            return A.Assign(target=left, value=right, line=t.line, col=t.col)
        return left

    def parse_or(self):
        left = self.parse_and()
        while self.at("or"):
            t = self.next()
            right = self.parse_and()
            left = A.Binary(op="or", left=left, right=right,
                            line=t.line, col=t.col)
        return left

    def parse_and(self):
        left = self.parse_equality()
        while self.at("and"):
            t = self.next()
            right = self.parse_equality()
            left = A.Binary(op="and", left=left, right=right,
                            line=t.line, col=t.col)
        return left

    def parse_equality(self):
        left = self.parse_comparison()
        while self.peek().kind in ("==", "!="):
            t = self.next()
            right = self.parse_comparison()
            left = A.Binary(op=t.kind, left=left, right=right,
                            line=t.line, col=t.col)
        return left

    def parse_comparison(self):
        left = self.parse_additive()
        while self.peek().kind in ("<", "<=", ">", ">="):
            t = self.next()
            right = self.parse_additive()
            left = A.Binary(op=t.kind, left=left, right=right,
                            line=t.line, col=t.col)
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.peek().kind in ("+", "-"):
            t = self.next()
            right = self.parse_multiplicative()
            left = A.Binary(op=t.kind, left=left, right=right,
                            line=t.line, col=t.col)
        return left

    def parse_multiplicative(self):
        left = self.parse_question()
        while self.peek().kind in ("*", "/", "%"):
            t = self.next()
            right = self.parse_question()
            left = A.Binary(op=t.kind, left=left, right=right,
                            line=t.line, col=t.col)
        return left

    def parse_question(self):
        e = self.parse_unary()
        while self.at("?"):
            t = self.next()
            e = A.Question(expr=e, line=t.line, col=t.col)
        return e

    def parse_unary(self):
        if self.at("not"):
            t = self.next()
            return A.Unary(op="not", expr=self.parse_unary(),
                           line=t.line, col=t.col)
        if self.at("-"):
            # unary minus only where a binary minus cannot appear
            t = self.next()
            return A.Unary(op="-", expr=self.parse_unary(),
                           line=t.line, col=t.col)
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while True:
            if self.at("."):
                self.next()
                nt = self.expect("IDENT", "member name")
                if self.at("("):
                    args = self.parse_args()
                    e = A.MethodCall(obj=e, name=nt.value, args=args,
                                     line=nt.line, col=nt.col)
                else:
                    e = A.Field(obj=e, name=nt.value, line=nt.line, col=nt.col)
            elif self.at("["):
                t = self.next()
                idx = self.parse_expr()
                self.expect("]", "']'")
                e = A.Index(obj=e, index=idx, line=t.line, col=t.col)
            else:
                break
        return e

    def parse_spawn(self):
        t = self.expect("spawn")
        block = self.parse_block()
        return A.Spawn(block=block, line=t.line, col=t.col)

    def parse_spawn_async(self):
        t = self.expect("spawn_async")
        block = self.parse_block()
        return A.SpawnAsync(block=block, line=t.line, col=t.col)

    def parse_nursery(self):
        t = self.expect("nursery")
        block = self.parse_block()
        return A.Nursery(block=block, line=t.line, col=t.col)

    def parse_context_decl(self):
        t = self.expect("context")
        name = self.expect("IDENT", "context name")
        self.expect(":", "':'")
        ty = self.parse_type()
        default = None
        if self.accept("="):
            default = self.parse_expr()
        return A.ContextDecl(name=name.value, type=ty, default=default,
                             line=t.line, col=t.col)

    def parse_provide(self):
        t = self.expect("provide")
        name = self.expect("IDENT", "context name")
        self.expect("=", "'='")
        value = self.parse_expr()
        block = self.parse_block()
        return A.Provide(name=name.value, value=value, block=block,
                         line=t.line, col=t.col)

    def parse_consume(self):
        t = self.expect("consume")
        name = self.expect("IDENT", "context name")
        return A.Consume(name=name.value, line=t.line, col=t.col)

    def parse_stream_decl(self):
        t = self.expect("stream")
        name = self.expect("IDENT", "stream name")
        self.expect(":", "':'")
        ty = self.parse_type()
        topic = None
        if self.accept("="):
            st = self.expect("STRING", "topic name string")
            topic = st.value
        return A.StreamDecl(name=name.value, type=ty, topic=topic,
                            line=t.line, col=t.col)

    def parse_primary(self):
        t = self.peek()
        if t.kind == "INT":
            self.next()
            return A.IntLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "FLOAT":
            self.next()
            return A.FloatLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "STRING":
            self.next()
            return A.StrLit(value=t.value, line=t.line, col=t.col)
        if t.kind in ("true", "false"):
            self.next()
            return A.BoolLit(value=(t.kind == "true"), line=t.line, col=t.col)
        if t.kind == "None":
            self.next()
            return A.NoneLit(line=t.line, col=t.col)
        if t.kind == "Some":
            self.next()
            self.expect("(", "'('")
            e = self.parse_expr()
            self.expect(")", "')'")
            return A.SomeExpr(expr=e, line=t.line, col=t.col)
        if t.kind == "Ok":
            self.next()
            self.expect("(", "'('")
            e = self.parse_expr()
            self.expect(")", "')'")
            return A.OkExpr(expr=e, line=t.line, col=t.col)
        if t.kind == "Err":
            self.next()
            self.expect("(", "'('")
            e = self.parse_expr()
            self.expect(")", "')'")
            return A.ErrExpr(expr=e, line=t.line, col=t.col)
        if t.kind == "this":
            self.next()
            return A.This(line=t.line, col=t.col)
        if t.kind == "super":
            self.next()
            return A.Super(line=t.line, col=t.col)
        if t.kind == "result":
            self.next()
            return A.ResultVar(line=t.line, col=t.col)
        if t.kind == "match":
            return self.parse_match()
        if t.kind == "if":
            return self.parse_if_expr()
        if t.kind == "spawn":
            return self.parse_spawn()
        if t.kind == "spawn_async":
            return self.parse_spawn_async()
        if t.kind == "nursery":
            return self.parse_nursery()
        if t.kind == "provide":
            return self.parse_provide()
        if t.kind == "consume":
            return self.parse_consume()
        if t.kind == "IDENT":
            self.next()
            if self.at("("):
                args = self.parse_args()
                return A.Call(func=A.Var(name=t.value, line=t.line, col=t.col),
                              args=args, line=t.line, col=t.col)
            return A.Var(name=t.value, line=t.line, col=t.col)
        if t.kind == "[":
            self.next()
            elems = []
            self.skip_newlines()
            while not self.at("]"):
                elems.append(self.parse_expr())
                if not self.accept(","):
                    break
            self.expect("]", "']'")
            return A.ListLit(elems=elems, line=t.line, col=t.col)
        if t.kind == "(":
            self.next()
            e = self.parse_expr()
            self.expect(")", "')'")
            return e
        raise ParseError("unexpected %s" % t.kind, t.line, t.col,
                         hint="expected an expression")

    def parse_args(self):
        self.expect("(", "'('")
        args = []
        self.skip_newlines()
        while not self.at(")"):
            name = None
            if (self.at("IDENT") and self.peek(1).kind == ":"
                    and self.peek(2).kind not in (":",)):
                # named argument — but not a slice or anything weird
                name = self.next().value
                self.next()  # :
            args.append((name, self.parse_expr()))
            if not self.accept(","):
                break
            self.skip_newlines()
        self.expect(")", "')'")
        return args

    def parse_match(self):
        t = self.expect("match")
        scrut = self.parse_expr()
        self.expect("{", "'{'")
        arms = []
        self.skip_newlines()
        while not self.at("}"):
            arms.append(self.parse_match_arm())
            self.skip_newlines()
            self.accept(",")
            self.skip_newlines()
        self.expect("}", "'}'")
        return A.Match(scrutinee=scrut, arms=arms, line=t.line, col=t.col)

    def parse_match_arm(self):
        pat = self.parse_pattern()
        self.expect("=>", "'=>'")
        expr = self.parse_expr()
        return A.MatchArm(pattern=pat, expr=expr, line=pat.line, col=pat.col)

    def parse_pattern(self):
        t = self.peek()
        if t.kind == "Some":
            self.next()
            self.expect("(", "'('")
            name = self.expect("IDENT", "binding name").value
            self.expect(")", "')'")
            return A.PatSome(name=name, line=t.line, col=t.col)
        if t.kind == "None":
            self.next()
            return A.PatNone(line=t.line, col=t.col)
        if t.kind == "Ok":
            self.next()
            self.expect("(", "'('")
            name = self.expect("IDENT", "binding name").value
            self.expect(")", "')'")
            return A.PatOk(name=name, line=t.line, col=t.col)
        if t.kind == "Err":
            self.next()
            self.expect("(", "'('")
            name = self.expect("IDENT", "binding name").value
            self.expect(")", "')'")
            return A.PatErr(name=name, line=t.line, col=t.col)
        if t.kind == "INT":
            self.next()
            return A.PatLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "FLOAT":
            self.next()
            return A.PatLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "STRING":
            self.next()
            return A.PatLit(value=t.value, line=t.line, col=t.col)
        if t.kind in ("true", "false"):
            self.next()
            return A.PatLit(value=(t.kind == "true"), line=t.line, col=t.col)
        if t.kind == "IDENT" and t.value == "_":
            self.next()
            return A.PatWild(line=t.line, col=t.col)
        if t.kind == "IDENT":
            self.next()
            return A.PatVar(name=t.value, line=t.line, col=t.col)
        raise ParseError("bad pattern", t.line, t.col,
                         hint="patterns: Some(x), None, Ok(x), Err(x), "
                              "literals, _, or a name")

    def parse_if_expr(self):
        t = self.expect("if")
        cond = self.parse_expr()
        then_b = self.parse_block()
        self.skip_newlines()
        else_b = None
        if self.at("else"):
            self.next()
            self.skip_newlines()
            if self.at("if"):
                # else-if as expression: wrap in a block
                inner = self.parse_if_expr()
                else_b = A.Block(stmts=[A.ExprStmt(expr=inner)],
                                 line=inner.line, col=inner.col)
            else:
                else_b = self.parse_block()
        else:
            raise ParseError("if used as expression needs else",
                             t.line, t.col)
        return A.IfExpr(cond=cond, then_b=then_b, else_b=else_b,
                        line=t.line, col=t.col)


# --------------------------------------------------------------------------
# JSX mini-parser: parses the raw source span of a `view { ... }` block.
# --------------------------------------------------------------------------
class JsxError(Exception):
    def __init__(self, message, line, col):
        super().__init__(message)
        self.line = line
        self.col = col


class JsxParser:
    def __init__(self, text, filename, base_line, base_col):
        self.text = text
        self.filename = filename
        self.pos = 0
        self.base_line = base_line
        self.base_col = base_col
        self.line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.line_starts.append(i + 1)

    def err(self, msg):
        line, col = self.loc()
        raise JsxError(msg, line, col)

    def loc(self):
        # absolute (line, col)
        nls = 0
        ls = self.line_starts
        i = 0
        while i + 1 < len(ls) and ls[i + 1] <= self.pos:
            i += 1
        nls = i
        if nls == 0:
            return self.base_line, self.base_col + self.pos
        return self.base_line + nls, self.pos - ls[nls] + 1

    def peek(self, n=1):
        return self.text[self.pos:self.pos + n]

    def eof(self):
        return self.pos >= len(self.text)

    def skip_ws(self):
        while not self.eof() and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def parse_children_top(self):
        children = self.parse_children(top=True)
        self.skip_ws()
        if not self.eof():
            self.err("unexpected %r in view block" % self.peek(10))
        return children

    def parse_children(self, top=False):
        children = []
        while True:
            self.skip_ws()
            if self.eof():
                if top:
                    break
                self.err("unterminated view block")
            if self.peek(2) == "</" or self.peek() == "}":
                break
            if self.peek() == "<":
                children.append(self.parse_element())
            elif self.peek() == "{":
                children.append(self.parse_braced_expr())
            else:
                txt = self.parse_text()
                if txt.strip():
                    line, col = self.loc()
                    children.append(A.JText(text=txt.strip(),
                                            line=line, col=col))
        return children

    def parse_text(self):
        start = self.pos
        while not self.eof() and self.peek() not in "<{":
            self.pos += 1
        return self.text[start:self.pos]

    def parse_braced_expr(self):
        line, col = self.loc()
        assert self.peek() == "{"
        span, end = self.match_braces(self.pos)
        self.pos = end + 1
        expr = self.sub_parse_expr(span, line, col)
        return A.JExpr(expr=expr, line=line, col=col)

    def match_braces(self, open_off):
        depth = 0
        i = open_off
        in_str = False
        while i < len(self.text):
            ch = self.text[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return self.text[open_off + 1:i], i
            i += 1
        self.err("unterminated { ... }")

    def sub_parse_expr(self, span, line, col):
        try:
            toks = Lexer(span, self.filename).tokenize()
        except LexError as e:
            raise JsxError(str(e), line, col)
        sub = Parser(toks, span, self.filename)
        # rebase sub-parser errors onto the view location
        try:
            e = sub.parse_expr()
        except ParseError as pe:
            raise JsxError(str(pe), line, col)
        if not sub.at("EOF"):
            t = sub.peek()
            raise JsxError("unexpected %s in expression" % t.kind, line, col)
        e.line, e.col = line, col
        return e

    def parse_element(self):
        line, col = self.loc()
        assert self.peek() == "<"
        self.pos += 1
        self.skip_ws()
        if self.peek(3) == "for" and not self.peek(4)[3:4].isalnum():
            return self.parse_for(line, col)
        name = self.parse_ident("tag name")
        is_component = name[0].isupper()
        attrs = []
        while True:
            self.skip_ws()
            if self.peek(2) == "/>":
                self.pos += 2
                return A.JElement(tag=name, attrs=attrs, children=[],
                                  is_component=is_component,
                                  line=line, col=col)
            if self.peek() == ">":
                self.pos += 1
                break
            attrs.append(self.parse_attr())
        children = self.parse_children()
        self.skip_ws()
        if self.peek(2) != "</":
            self.err("expected </%s>" % name)
        self.pos += 2
        self.skip_ws()
        cname = self.parse_ident("closing tag")
        if cname != name:
            self.err("mismatched tags <%s>...</%s>" % (name, cname))
        self.skip_ws()
        if self.peek() != ">":
            self.err("expected '>'")
        self.pos += 1
        return A.JElement(tag=name, attrs=attrs, children=children,
                          is_component=is_component, line=line, col=col)

    def parse_for(self, line, col):
        self.pos += 3  # 'for'
        self.skip_ws()
        var = self.parse_ident("loop variable")
        self.skip_ws()
        if self.peek(2) != "in":
            self.err("expected 'in' in <for>")
        self.pos += 2
        self.skip_ws()
        if self.peek() != "{":
            # allow a bare identifier: <for item in items>
            var2 = self.parse_ident("iterable")
            self.skip_ws()
            if self.peek() != ">":
                self.err("expected '>'")
            self.pos += 1
            children = self.parse_children()
            self.skip_ws()
            if self.peek(2) != "</":
                self.err("expected </for>")
            self.pos += 2
            self.skip_ws()
            cname = self.parse_ident("closing tag")
            if cname != "for":
                self.err("mismatched <for>...</%s>" % cname)
            self.skip_ws()
            if self.peek() != ">":
                self.err("expected '>'")
            self.pos += 1
            line2, col2 = self.loc()
            return A.JFor(var=var, iter=A.Var(name=var2, line=line2, col=col2),
                          children=children, line=line, col=col)
        jexpr = self.parse_braced_expr()
        self.skip_ws()
        if self.peek() != ">":
            self.err("expected '>'")
        self.pos += 1
        children = self.parse_children()
        self.skip_ws()
        if self.peek(2) != "</":
            self.err("expected </for>")
        self.pos += 2
        self.skip_ws()
        cname = self.parse_ident("closing tag")
        if cname != "for":
            self.err("mismatched <for>...</%s>" % cname)
        self.skip_ws()
        if self.peek() != ">":
            self.err("expected '>'")
        self.pos += 1
        return A.JFor(var=var, iter=jexpr.expr, children=children,
                      line=line, col=col)

    def parse_ident(self, what):
        start = self.pos
        while not self.eof() and (self.text[self.pos].isalnum()
                                  or self.text[self.pos] in "_-"):
            # allow '-' so we fail gracefully on bad tags like <my-tag>
            self.pos += 1
        name = self.text[start:self.pos]
        if not name or not (name[0].isalpha() or name[0] == "_"):
            self.err("expected %s" % what)
        if "-" in name:
            self.err("tag names use CamelCase, not kebab-case: %s" % name)
        return name

    def parse_attr(self):
        line, col = self.loc()
        name = self.parse_ident("attribute name")
        self.skip_ws()
        value = None
        if self.peek() == "=":
            self.pos += 1
            self.skip_ws()
            if self.peek() == "{":
                value = self.parse_braced_expr().expr
            elif self.peek() == '"':
                value = self.parse_string()
            else:
                self.err("attribute value must be \"...\" or {...}")
        else:
            value = A.BoolLit(value=True, line=line, col=col)
        return A.JAttr(name=name, value=value, line=line, col=col)

    def parse_string(self):
        line, col = self.loc()
        assert self.peek() == '"'
        self.pos += 1
        out = []
        while not self.eof() and self.peek() != '"':
            if self.peek() == "\\":
                self.pos += 1
                esc = self.peek()
                mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
                if esc not in mapping:
                    self.err("bad escape")
                out.append(mapping[esc])
                self.pos += 1
            else:
                out.append(self.peek())
                self.pos += 1
        if self.eof():
            self.err("unterminated string")
        self.pos += 1
        return A.StrLit(value="".join(out), line=line, col=col)


def parse_source(source, filename="<input>"):
    toks = Lexer(source, filename).tokenize()
    return Parser(toks, source, filename).parse_program()
