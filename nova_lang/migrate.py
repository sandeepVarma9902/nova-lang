# migrate.py — EXPERIMENTAL mechanical Java/JSX -> Nova migration.
#
# `nova migrate --from java|jsx <file>` converts the most mechanical subset
# of Java / React-JSX to Nova and emits a WARNING (with source line number)
# for every construct it cannot convert. Nothing is silently dropped:
# skipped constructs are preserved as `// UNCONVERTED` / `// WARNING`
# comments in the output, and every one also lands in the warnings list
# (printed to stderr by the CLI).
#
# Honest limits: this is a mechanical mapper, not a transpiler. Effect
# annotations are guessed (print -> effect), capability threading
# (IO/Net params) is yours to wire, and checked exceptions are flattened
# to Result[_, String] — you pick the real error type. The mapping tables
# and warning catalog live in docs/v2-migrate.md.

import re

# ---------------------------------------------------------------------------
# shared infrastructure
# ---------------------------------------------------------------------------

class _Warnings(object):
    """Collects 'line N: message' warnings; the CLI prints them to stderr."""

    def __init__(self):
        self.items = []

    def add(self, line_no, msg):
        self.items.append("line %d: %s" % (line_no, msg))

    def __len__(self):
        return len(self.items)


class _Src(object):
    """A source span with its starting line, for warning line numbers."""

    def __init__(self, text, base_line=1):
        self.text = text
        self.base_line = base_line

    def line(self, pos):
        return self.base_line + self.text.count("\n", 0, max(0, pos))


def _find_matching(s, open_i, open_ch="{", close_ch="}"):
    """Index of the close bracket matching s[open_i]; -1 if unbalanced.

    String-aware: skips '...', "..." (with \\ escapes). Treats Nova/Java
    generics-free code; good enough for mechanical migration.
    """
    depth = 0
    i = open_i
    n = len(s)
    in_str = None
    while i < n:
        ch = s[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _strip_comments(src, jsx_braces=False):
    """Remove //, /* */ (and {/* */} when jsx_braces); keep newlines so
    positions still map 1:1 to original line numbers."""
    out = []
    i, n = 0, len(src)
    in_str = None
    while i < n:
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 1
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue  # the newline itself is appended by the loop
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n
                                 and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        if jsx_braces and ch == "{" and src.startswith("{/*", i):
            j = src.find("*/}", i)
            if j == -1:
                j = n - 3
            out.append(src[i:j + 3].count("\n") * "\n")
            i = j + 3
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _protect_strings(e):
    """Split an expression into (is_string, text) segments so rewrites
    never touch string-literal contents."""
    segs = []
    i, n = 0, len(e)
    buf = []
    while i < n:
        ch = e[i]
        if ch in ("'", '"', "`"):
            if buf:
                segs.append((False, "".join(buf)))
                buf = []
            quote = ch
            j = i + 1
            lit = [ch]
            while j < n:
                lit.append(e[j])
                if e[j] == "\\" and j + 1 < n:
                    lit.append(e[j + 1])
                    j += 2
                    continue
                if e[j] == quote:
                    j += 1
                    break
                j += 1
            segs.append((True, "".join(lit)))
            i = j
        else:
            buf.append(ch)
            i += 1
    if buf:
        segs.append((False, "".join(buf)))
    return segs


def _split_top(body, base_line):
    """Split a method/class body into top-level statements.

    Returns a list of dicts:
      {"kind": "simple", "text": ..., "line": N}              # ends with ';'
      {"kind": "compound", "header": ..., "block": "{...}",
       "line": N, "chain": [...]}                             # header + block
    `chain` holds else/catch/finally/while continuations for
    if/try/do statements.
    """
    src = _Src(body, base_line)
    stmts = []
    i, n = 0, len(body)
    while i < n:
        # skip whitespace
        while i < n and body[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if body[i] == ";":  # stray semicolon
            i += 1
            continue
        line = src.line(i)
        # find next ';' or '{' at depth 0 (paren/brace/bracket aware)
        j = i
        pd = bd = 0
        in_str = None
        kind = None
        k = -1
        while j < n:
            ch = body[j]
            if in_str:
                if ch == "\\":
                    j += 1
                elif ch == in_str:
                    in_str = None
            elif ch in ("'", '"', "`"):
                in_str = ch
            elif ch == "(":
                pd += 1
            elif ch == ")":
                pd -= 1
            elif ch == "[":
                bd += 1
            elif ch == "]":
                bd -= 1
            elif ch == "{" and pd == 0 and bd == 0:
                kind, k = "compound", j
                break
            elif ch == ";" and pd == 0 and bd == 0:
                kind, k = "simple", j
                break
            j += 1
        if kind is None:
            tail = body[i:].strip()
            if tail:
                stmts.append({"kind": "simple", "text": tail, "line": line})
            break
        if kind == "simple":
            stmts.append({"kind": "simple",
                          "text": body[i:k].strip(), "line": line})
            i = k + 1
            continue
        # compound: header + balanced block
        header = body[i:k].strip()
        close = _find_matching(body, k)
        if close == -1:
            stmts.append({"kind": "simple", "text": body[i:].strip(),
                          "line": line})
            break
        block = body[k:close + 1]
        i = close + 1
        chain = []
        # else-if / else / catch / finally / do-while continuations
        while True:
            save = i
            while i < n and body[i] in " \t\r\n":
                i += 1
            if body.startswith("else", i) and not (
                    i + 4 < n and (body[i + 4].isalnum() or body[i + 4] == "_")):
                i += 4
                while i < n and body[i] in " \t\r\n":
                    i += 1
                if body.startswith("if", i) and not (
                        i + 2 < n and (body[i + 2].isalnum()
                                       or body[i + 2] == "_")):
                    # else if (cond) { ... }
                    p = body.find("(", i)
                    q = _find_matching(body, p, "(", ")")
                    cond = body[p + 1:q]
                    b2 = _find_matching(body, q + 1)
                    chain.append(("elseif", cond.strip(),
                                  body[q + 1:b2 + 1], src.line(p)))
                    i = b2 + 1
                    continue
                if i < n and body[i] == "{":
                    b2 = _find_matching(body, i)
                    chain.append(("else", None, body[i:b2 + 1],
                                  src.line(i)))
                    i = b2 + 1
                    continue
                i = save
                break
            if header == "try" or (chain and chain[-1][0] == "catch"):
                for kw in ("catch", "finally"):
                    if body.startswith(kw, i) and not (
                            i + len(kw) < n and (
                                body[i + len(kw)].isalnum()
                                or body[i + len(kw)] == "_")):
                        i += len(kw)
                        while i < n and body[i] in " \t\r\n":
                            i += 1
                        decl = None
                        if kw == "catch" and i < n and body[i] == "(":
                            q = _find_matching(body, i, "(", ")")
                            decl = body[i + 1:q].strip()
                            i = q + 1
                            while i < n and body[i] in " \t\r\n":
                                i += 1
                        if i < n and body[i] == "{":
                            b2 = _find_matching(body, i)
                            chain.append((kw, decl, body[i:b2 + 1],
                                          src.line(i)))
                            i = b2 + 1
                            break
                else:
                    i = save
                    break
                continue
            if header == "do":
                if body.startswith("while", i) and not (
                        i + 5 < n and (body[i + 5].isalnum()
                                       or body[i + 5] == "_")):
                    p = body.find("(", i)
                    q = _find_matching(body, p, "(", ")")
                    cond = body[p + 1:q]
                    # consume the trailing ';'
                    r = q + 1
                    while r < n and body[r] in " \t\r\n":
                        r += 1
                    if r < n and body[r] == ";":
                        r += 1
                    chain.append(("dowhile", cond.strip(), None,
                                  src.line(p)))
                    i = r
                    break
            i = save
            break
        stmts.append({"kind": "compound", "header": header, "block": block,
                      "line": line, "chain": chain})
    return stmts


def _inner(block):
    """Text inside the outer braces of a '{ ... }' block."""
    block = block.strip()
    if block.startswith("{") and block.endswith("}"):
        return block[1:-1]
    return block


def _strip_annotations(header):
    """Pull leading @Annotations off a member header; return (annotations,
    rest)."""
    anns = []
    h = header.strip()
    while h.startswith("@"):
        m = re.match(r"@\w+(?:\s*\([^)]*\))?\s*", h)
        if not m:
            break
        anns.append(m.group(0).strip())
        h = h[m.end():]
    return anns, h


_MODIFIERS = {"public", "private", "protected", "static", "final",
              "abstract", "synchronized", "transient", "volatile",
              "native", "default"}


def _strip_modifiers(text):
    mods = []
    rest = text.strip()
    while True:
        m = re.match(r"(\w+)\s+", rest)
        if m and m.group(1) in _MODIFIERS:
            mods.append(m.group(1))
            rest = rest[m.end():]
        else:
            break
    return mods, rest


def _unconverted(text, indent="", label="UNCONVERTED", width=100):
    """Render dropped source as // comments; every line commented so a
    multi-line original can never leak uncommented code into the output."""
    raw = text.strip().split("\n")
    first = raw[0][:width]
    if len(raw[0]) > width:
        first += " ..."
    lines = ["%s// %s: %s" % (indent, label, first)]
    for l in raw[1:]:
        lines.append("%s//   %s" % (indent, l.strip()[:width]))
    return lines

# ---------------------------------------------------------------------------
# Java -> Nova
# ---------------------------------------------------------------------------

JAVA_TYPES = {
    "int": "Int", "long": "Int", "short": "Int", "byte": "Int",
    "double": "Float", "float": "Float",
    "String": "String", "boolean": "Bool", "void": None,
    "Integer": "Int", "Long": "Int", "Double": "Float", "Boolean": "Bool",
    "Object": "Any", "char": "String", "Character": "String",
}

_ZERO_VALUE = {
    "Int": "0", "Float": "0.0", "String": '""', "Bool": "false",
    "None": "None",
}


def _warn_unknown_java_type(warn, line_no, jt):
    warn.add(line_no, "type '%s' has no Nova mapping — emitted as Any; "
                      "annotate by hand" % jt)


def java_type(jt, warn=None, line_no=0):
    """Map a Java type to a Nova type; unknown -> Any (+ warning)."""
    jt = jt.strip()
    # strip generics noise like '? extends Foo'
    jt = re.sub(r"\?\s+extends\s+\w+", "Any", jt)
    jt = re.sub(r"\?\s+super\s+\w+", "Any", jt)
    m = re.match(r"(?:java\.util\.)?(?:List|ArrayList|LinkedList|Collection)"
                 r"<\s*([^<>]+?)\s*>$", jt)
    if m:
        inner = java_type(m.group(1), warn, line_no)
        return "List[%s]" % inner
    m = re.match(r"(?:java\.util\.)?(?:Set|HashSet|TreeSet)"
                 r"<\s*([^<>]+?)\s*>$", jt)
    if m:
        if warn is not None:
            warn.add(line_no, "Set -> List (dedup semantics lost); "
                              "verify by hand")
        return "List[%s]" % java_type(m.group(1), warn, line_no)
    m = re.match(r"(?:java\.util\.)?Optional<\s*([^<>]+?)\s*>$", jt)
    if m:
        return "Option[%s]" % java_type(m.group(1), warn, line_no)
    if re.match(r"(?:java\.util\.)?(?:Map|HashMap|TreeMap)\s*<", jt):
        if warn is not None:
            warn.add(line_no, "Map has no Nova equivalent (v1) — emitted "
                              "as Any; model as a data class or List of "
                              "pairs by hand")
        return "Any"
    m = re.match(r"(\w+)\[\]$", jt)
    if m:
        return "List[%s]" % java_type(m.group(1), warn, line_no)
    # other generics: erase with a warning
    m = re.match(r"(\w+)\s*<.*>$", jt, re.S)
    if m:
        if warn is not None:
            warn.add(line_no, "generics on '%s' are erased — emitted as "
                              "Any" % m.group(1))
        return "Any"
    if jt in JAVA_TYPES:
        return JAVA_TYPES[jt]
    if warn is not None:
        _warn_unknown_java_type(warn, line_no, jt)
    return "Any"


def _zero_value(nt):
    if nt in _ZERO_VALUE:
        return _ZERO_VALUE[nt]
    if nt.startswith("List["):
        return "[]"
    if nt.startswith("Option["):
        return "None"
    return None


# --- expression translation -----------------------------------------------

def _split_top_level(e, ch):
    """Split e on ch at paren/brace/bracket depth 0, string-aware."""
    parts, buf = [], []
    pd = bd = cd = 0
    in_str = None
    i, n = 0, len(e)
    while i < n:
        c = e[i]
        if in_str:
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(e[i + 1])
                i += 1
            elif c == in_str:
                in_str = None
        elif c in ("'", '"', "`"):
            in_str = c
            buf.append(c)
        elif c == ch and pd == 0 and bd == 0 and cd == 0:
            parts.append("".join(buf))
            buf = []
        else:
            if c == "(":
                pd += 1
            elif c == ")":
                pd -= 1
            elif c == "[":
                bd += 1
            elif c == "]":
                bd -= 1
            elif c == "{":
                cd += 1
            elif c == "}":
                cd -= 1
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _find_top_ternary(e):
    """Locate a top-level '?' (not part of '?.') and its matching ':'."""
    pd = bd = cd = 0
    in_str = None
    i, n = 0, len(e)
    while i < n:
        c = e[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == in_str:
                in_str = None
        elif c in ("'", '"', "`"):
            in_str = c
        elif c == "(":
            pd += 1
        elif c == ")":
            pd -= 1
        elif c == "[":
            bd += 1
        elif c == "]":
            bd -= 1
        elif c == "{":
            cd += 1
        elif c == "}":
            cd -= 1
        elif c == "?" and pd == 0 and bd == 0 and cd == 0:
            if i + 1 < n and e[i + 1] == ".":
                i += 1
            else:
                # find matching ':' at depth 0
                j = i + 1
                pd2 = bd2 = cd2 = 0
                in2 = None
                while j < n:
                    d = e[j]
                    if in2:
                        if d == "\\":
                            j += 1
                        elif d == in2:
                            in2 = None
                    elif d in ("'", '"', "`"):
                        in2 = d
                    elif d == "(":
                        pd2 += 1
                    elif d == ")":
                        pd2 -= 1
                    elif d == "[":
                        bd2 += 1
                    elif d == "]":
                        bd2 -= 1
                    elif d == "{":
                        cd2 += 1
                    elif d == "}":
                        cd2 -= 1
                    elif d == ":" and pd2 == 0 and bd2 == 0 and cd2 == 0:
                        return i, j
                    j += 1
                return i, -1
        i += 1
    return -1, -1


def _balanced_call_args(text, open_paren):
    """Given text and the index of '(', return (args, close_index)."""
    close = _find_matching(text, open_paren, "(", ")")
    if close == -1:
        return None, -1
    return _split_top_level(text[open_paren + 1:close], ","), close


def java_expr(e, warn, line_no, list_vars=None, static_classes=(),
              folded=None):
    """Translate a Java expression to Nova. Best-effort; every lossy step
    emits a warning with the source line."""
    list_vars = list_vars or set()
    folded = folded or {}
    e = e.strip()
    if not e:
        return e

    # ternary first, on the raw text: _find_top_ternary is string-aware,
    # and splitting into segments first would separate '?' from ':'
    qi, ci = _find_top_ternary(e)
    if qi != -1 and ci != -1:
        return "if %s { %s } else { %s }" % (
            java_expr(e[:qi].strip(), warn, line_no, list_vars,
                      static_classes, folded),
            java_expr(e[qi + 1:ci].strip(), warn, line_no, list_vars,
                      static_classes, folded),
            java_expr(e[ci + 1:].strip(), warn, line_no, list_vars,
                      static_classes, folded))

    # char literal -> String literal
    def char_fix(m):
        warn.add(line_no, "char literal %s -> String (Nova has no char)"
                 % m.group(0))
        inner = m.group(1).encode().decode("unicode_escape", errors="ignore") \
            if "\\" in m.group(1) else m.group(1)
        return '"%s"' % inner.replace('"', '\\"')
    e = re.sub(r"'((?:\\.|[^'\\])*)'", char_fix, e)

    segs = _protect_strings(e)
    out = []
    for is_str, seg in segs:
        if is_str:
            out.append(seg)
            continue
        out.append(_java_expr_code(seg, warn, line_no, list_vars,
                                   static_classes, folded))
    return "".join(out).strip()


def _java_expr_code(seg, warn, line_no, list_vars, static_classes,
                    folded):
    s = seg

    # null -> None
    if re.search(r"\bnull\b", s):
        warn.add(line_no, "'null' -> None; the binding must be Option[_] — "
                          "verify")
    s = re.sub(r"\bnull\b", "None", s)

    # numeric suffixes
    if re.search(r"\b\d+[lL]\b", s):
        warn.add(line_no, "long literal suffix stripped (Nova Int is i64)")
    s = re.sub(r"\b(\d+)[lL]\b", r"\1", s)
    if re.search(r"\b\d+\.\d+[fF]\b", s):
        warn.add(line_no, "float literal suffix stripped")
    s = re.sub(r"\b(\d+\.\d+)[fF]\b", r"\1", s)
    if re.search(r"\b0[xX][0-9a-fA-F]+\b", s):
        warn.add(line_no, "hex literal kept as-is — verify Nova accepts it")

    # new X(...) -> X(...); collections first
    def new_fix(m):
        cls, args = m.group(1).split("<")[0], m.group(2)
        if cls in ("ArrayList", "LinkedList"):
            if args.strip():
                warn.add(line_no, "new %s(args) -> [] — copy-construction "
                                  "dropped; verify" % cls)
            return "[]"
        if cls in ("HashMap", "TreeMap", "HashSet", "TreeSet"):
            warn.add(line_no, "new %s(...) has no Nova equivalent — "
                              "emitted as empty List; rewrite by hand" % cls)
            return "[]"
        if re.match(r"\w+\[\]$", cls) or args.strip().startswith("["):
            pass
        return "%s(%s)" % (cls.split(".")[-1], args)
    s = re.sub(r"\bnew\s+([\w.]+(?:<[^<>]*>)?)\s*\(([^()]*)\)",
               new_fix, s)
    if re.search(r"\bnew\s+\w+\s*\[", s):
        warn.add(line_no, "array creation 'new T[n]' -> growable List []; "
                          "size semantics differ — verify")
        s = re.sub(r"\bnew\s+\w+(?:<[^<>]*>)?\s*\[[^\]]*\]", "[]", s)

    # folded JavaBean accessors -> field access:
    # obj.getX() -> obj.x ; obj.setX(v) -> obj.x = v
    if folded:
        for acc, field in folded.items():
            if acc.startswith("set"):
                pat = re.compile(r"([\w\.\[\]]+)\.%s\s*\("
                                 % re.escape(acc))
                parts, idx = [], 0
                while True:
                    m2 = pat.search(s[idx:])
                    if not m2:
                        break
                    p2 = idx + m2.end() - 1
                    args2, close2 = _balanced_call_args(s, p2)
                    if args2 is None or len(args2) != 1:
                        parts.append(s[idx:idx + m2.end()])
                        idx += m2.end()
                        continue
                    parts.append(s[idx:idx + m2.start()])
                    parts.append("%s.%s = %s"
                                 % (m2.group(1), field,
                                    args2[0].strip()))
                    idx = close2 + 1
                parts.append(s[idx:])
                s = "".join(parts)
            else:
                s = re.sub(r"([\w\.\[\]]+)\.%s\s*\(\s*\)" % re.escape(acc),
                           r"\1.%s" % field, s)

    # System.out.println / print
    s = s.replace("System.out.println(", "print(")
    if "System.out.print(" in s:
        warn.add(line_no, "System.out.print -> print (Nova always "
                          "newlines)")
        s = s.replace("System.out.print(", "print(")
    if "System.err" in s:
        warn.add(line_no, "System.err -> print; Nova has one console "
                          "channel")

    # Optional.of / Optional.empty
    s = re.sub(r"\bOptional\.of\s*\(", "Some(", s)
    if re.search(r"\bOptional\.empty\s*\(\s*\)", s):
        warn.add(line_no, "Optional.empty() -> None; the binding must be "
                          "Option[_]")
    s = re.sub(r"\bOptional\.empty\s*\(\s*\)", "None", s)
    if re.search(r"\bOptional\.ofNullable\s*\(", s):
        warn.add(line_no, "Optional.ofNullable -> Some(...); None-case "
                          "handling is yours to add")

    # String/collection conveniences
    def len_fix(m):
        recv = m.group(1)
        return "len(%s)" % recv
    s = re.sub(r"([\w\)\]]+(?:\.[\w\)\]]+)*)\.(?:length|size)\(\)", len_fix, s)
    s = re.sub(r"([\w\)\]]+(?:\.[\w\)\]]+)*)\.length\b", len_fix, s)

    # list.add(x) -> list.push(x) for known List vars; else warn
    def add_fix(m):
        recv, args = m.group(1), m.group(2)
        base = recv.split(".")[0].split("[")[0]
        if base in list_vars:
            return "%s.push(%s)" % (recv, args)
        warn.add(line_no, ".add() kept as .push() on '%s' — verify it is "
                          "a List" % recv)
        return "%s.push(%s)" % (recv, args)
    s = re.sub(r"([\w\.\[\]]+)\.add\s*\(([^()]*)\)", add_fix, s)

    # list.get(i) -> list[i] for known List vars; else warn
    def get_fix(m):
        recv, args = m.group(1), m.group(2)
        base = recv.split(".")[0].split("[")[0]
        if base in list_vars:
            return "%s[%s]" % (recv, args)
        warn.add(line_no, ".get() on '%s' -> [i]; verify it is indexable"
                 % recv)
        return "%s[%s]" % (recv, args)
    s = re.sub(r"([\w\.\[\]]+)\.get\s*\(([^()]*)\)", get_fix, s)

    # a.equals(b) -> (a == b); manual loop because the argument may
    # itself contain parens (re.sub can't span the balanced call)
    parts = []
    idx = 0
    while True:
        m = re.search(r"([\w\.\[\]]+)\.equals\s*\(", s[idx:])
        if not m:
            break
        p = idx + m.end() - 1  # index of the '('
        args, close = _balanced_call_args(s, p)
        if args is None or len(args) != 1:
            parts.append(s[idx:idx + m.end()])
            idx += m.end()
            continue
        parts.append(s[idx:idx + m.start()])
        parts.append("(%s == %s)" % (m.group(1), args[0].strip()))
        idx = close + 1
    parts.append(s[idx:])
    s = "".join(parts)

    # x.toString() -> str(x)
    s = re.sub(r"([\w\.\[\]\(\)]+)\.toString\s*\(\s*\)", r"str(\1)", s)
    s = re.sub(r"\bString\.valueOf\s*\(", "str(", s)
    s = re.sub(r"\bInteger\.parseInt\s*\(", "int(", s)
    s = re.sub(r"\bLong\.parseLong\s*\(", "int(", s)
    s = re.sub(r"\bDouble\.parseDouble\s*\(", "float(", s)
    if re.search(r"\bBoolean\.parseBoolean\s*\(", s):
        warn.add(line_no, "Boolean.parseBoolean(s) -> (s == \"true\")")
    s = re.sub(r"\bBoolean\.parseBoolean\s*\(([^()]*)\)", r'(\1 == "true")',
               s)

    # x.isEmpty() -> len(x) == 0
    s = re.sub(r"([\w\.\[\]]+)\.isEmpty\s*\(\s*\)", r"(len(\1) == 0)", s)
    if re.search(r"\.\s*(?:contains|indexOf|subList|toArray|stream)\s*\(",
                 s):
        warn.add(line_no, "Java collection method kept as-is — verify a "
                          "Nova equivalent exists")

    # static class qualification: Foo.bar() -> bar()
    for cls in static_classes:
        s = re.sub(r"\b%s\s*\.\s*" % re.escape(cls), "", s)

    # casts: (Type) x -> x
    def cast_fix(m):
        t = m.group(1)
        if t in JAVA_TYPES or re.match(r"List|Option|String|Int|Float|Bool",
                                       t):
            warn.add(line_no, "cast '(%s)' dropped — Nova has no casts; "
                              "verify the value really is %s" % (t, t))
            return ""
        return m.group(0)
    s = re.sub(r"\(\s*([A-Z]\w*(?:<[^<>]*>)?(?:\[\])?)\s*\)\s*", cast_fix, s)

    # instanceof -> false + warning
    if "instanceof" in s:
        warn.add(line_no, "'instanceof' has no Nova equivalent — emitted "
                          "as false; rewrite with a type test by hand")
        s = re.sub(r"[\w\.\(\)\[\]]+\s+instanceof\s+[\w<>\.]+", "false", s)

    # ClassName.class -> warning
    if re.search(r"\b\w+\.class\b", s):
        warn.add(line_no, ".class literal has no Nova equivalent")

    # method references
    if "::" in s:
        warn.add(line_no, "method reference '::' — Nova has no first-class "
                          "functions; rewrite by hand")

    # boolean operators
    s = s.replace("&&", " and ")
    s = s.replace("||", " or ")
    s = re.sub(r"!(?!=)", "not ", s)
    if "..." in s:
        warn.add(line_no, "spread '...' has no Nova equivalent — dropped; "
                          "rewrite by hand")
        s = s.replace("...", "")
    s = re.sub(r"\btrue\b", "true", s)
    s = re.sub(r"\bfalse\b", "false", s)

    # normalize whitespace (no strip: the caller joins segments and
    # strips once, so spacing around string literals survives)
    s = re.sub(r"[ \t]+", " ", s)
    return s

# --- statement translation ------------------------------------------------

def _translate_cond(header, warn, line_no, list_vars, static_classes,
                    folded=None):
    """Translate the '(...)' of if/while from a statement header."""
    p = header.find("(")
    if p == -1:
        return ""
    q = _find_matching(header, p, "(", ")")
    cond = header[p + 1:q] if q != -1 else header[p + 1:]
    return java_expr(cond, warn, line_no, list_vars, static_classes,
                     folded)


class _JavaMethod(object):
    def __init__(self):
        self.name = ""
        self.params = []        # (name, nova_type)
        self.ret = None         # Nova type or None (void)
        self.body = ""
        self.line = 0
        self.mods = []
        self.annotations = []
        self.throws = None
        self.is_ctor = False


def java_block(body, warn, base_line, list_vars=None, static_classes=(),
               in_result_fn=False, folded=None):
    """Translate a Java '{ ... }' body to Nova statements (list of lines)."""
    list_vars = set(list_vars or set())
    lines = []
    for st in _split_top(body, base_line):
        lines.extend(java_stmt(st, warn, list_vars, static_classes,
                               in_result_fn, folded))
    return lines


def java_stmt(st, warn, list_vars, static_classes,
              in_result_fn=False, folded=None):
    """Translate one split statement to Nova lines."""
    lines = []
    ln = st["line"]

    def ex(e):
        return java_expr(e, warn, ln, list_vars, static_classes, folded)

    if st["kind"] == "simple":
        text = st["text"].strip()
        if not text:
            return []
        # lambda / method-ref statements cannot be expressed
        if "->" in text and not text.strip().startswith("case"):
            warn.add(ln, "lambda '->' — Nova has no first-class functions; "
                         "statement dropped")
            lines.extend(_unconverted(text, label="UNCONVERTED (lambda)"))
            return lines
        if "::" in text:
            warn.add(ln, "method reference '::' — Nova has no first-class "
                         "functions; statement dropped")
            lines.extend(_unconverted(text, label="UNCONVERTED (method ref)"))
            return lines
        # return
        m = re.match(r"return\s*(.*)$", text, re.S)
        if m:
            val = m.group(1).strip()
            lines.append("return %s" % ex(val) if val else "return")
            return lines
        # throw
        m = re.match(r"throw\s+(.*)$", text, re.S)
        if m:
            if in_result_fn:
                warn.add(ln, "'throw' -> 'return Err(...)' (checked "
                             "exceptions flattened to Result[_, String])")
                msg = re.sub(r"^new\s+\w+\s*\(", "(", m.group(1).strip())
                lines.append("return Err%s" % msg)
            else:
                warn.add(ln, "'throw' outside a 'throws' method — Nova has "
                             "no exceptions; statement dropped")
                lines.extend(_unconverted(text, label="UNCONVERTED (throw)"))
            return lines
        # assert
        m = re.match(r"assert\s+(.*?)(?:\s*:\s*(.*))?$", text, re.S)
        if m:
            warn.add(ln, "'assert' mapped to Nova assert(); semantics may "
                         "differ — verify")
            lines.append("assert(%s)" % ex(m.group(1)))
            return lines
        # break / continue
        if text in ("break", "continue"):
            lines.append(text)
            return lines
        # local declaration: [final] Type v [= e], ...
        mods, rest = _strip_modifiers(text)
        dm = re.match(r"([\w<>\[\].,\s\?]+?)\s+(\w.+)$", rest, re.S)
        if dm and "final" in mods or (dm and _looks_like_decl(dm, rest)):
            jt, decls = dm.group(1).strip(), dm.group(2)
            # sanity: type part must not contain '=' or spaces-run junk
            if "=" not in jt:
                nt = java_type(jt, warn, ln)
                is_final = "final" in mods
                kw = "let" if is_final else "var"
                for decl in _split_top_level(decls, ","):
                    decl = decl.strip()
                    vm = re.match(r"(\w+)\s*(=\s*(.*))?$", decl, re.S)
                    if not vm:
                        warn.add(ln, "could not parse declarator '%s' — "
                                     "dropped" % decl)
                        lines.extend(_unconverted(decl + ";"))
                        continue
                    vname, init = vm.group(1), vm.group(3)
                    if nt and nt.startswith("List[") or (
                            nt == "Any" and init and init.strip() == "[]"):
                        list_vars.add(vname)
                    ann = ": %s" % nt if nt else ""
                    if init is not None:
                        lines.append("%s %s%s = %s"
                                     % (kw, vname, ann, ex(init)))
                    else:
                        zv = _zero_value(nt) if nt else None
                        if zv is None:
                            warn.add(ln, "no initializer for '%s' — Nova "
                                         "requires one; using 0 and hoping"
                                         % vname)
                            zv = "0"
                        else:
                            warn.add(ln, "no initializer for '%s' — Nova "
                                         "requires one; zero value %s used"
                                         % (vname, zv))
                        lines.append("%s %s%s = %s" % (kw, vname, ann, zv))
                return lines
        # assignment / compound assignment / inc-dec
        m = re.match(r"([\w\.\[\]]+)\s*(\+\+|--)\s*$", text)
        if m:
            op = "+" if m.group(2) == "++" else "-"
            lines.append("%s = %s %s 1" % (m.group(1), m.group(1), op))
            return lines
        m = re.match(r"([\w\.\[\]]+)\s*([+\-*/%])=\s*(.*)$", text, re.S)
        if m:
            lines.append("%s = %s %s (%s)"
                         % (m.group(1), m.group(1), m.group(2),
                            ex(m.group(3))))
            return lines
        m = re.match(r"([\w\.\[\]]+)\s*=\s*(.*)$", text, re.S)
        if m:
            lines.append("%s = %s" % (m.group(1), ex(m.group(2))))
            return lines
        # expression statement
        lines.append(ex(text))
        return lines

    # ---- compound statements ----
    header = st["header"].strip()
    block = st["block"]
    chain = st.get("chain", [])

    def sub_block(b, bline):
        return java_block(_inner(b), warn, bline, set(list_vars),
                          static_classes, in_result_fn, folded)

    # if / else-if / else
    if re.match(r"if\s*\(", header):
        # null-check special case on the RAW condition (translation turns
        # null into None, so test before translating)
        praw = header.find("(")
        qraw = _find_matching(header, praw, "(", ")") if praw != -1 else -1
        raw_cond = header[praw + 1:qraw].strip() if qraw != -1 else ""
        cond = _translate_cond(header, warn, ln, list_vars, static_classes)
        mnull = re.match(r"(\w+)\s*(==|!=)\s*null$", raw_cond)
        if mnull:
            var, op = mnull.group(1), mnull.group(2)
            warn.add(ln, "null check on '%s' — model as Option and handle "
                         "with match (migration-guide.md Recipe 4)"
                         % var)
            lines.append("// WARNING: Java null check has no direct "
                         "translation — original preserved below")
            if op == "!=":
                lines.append("// match %s {" % var)
                lines.append("//   Some(v) => { /* was: if-branch */ },")
                lines.append("//   None => { /* was: else-branch */ }")
                lines.append("// }")
            else:
                lines.append("// match %s {" % var)
                lines.append("//   None => { /* was: if-branch */ },")
                lines.append("//   Some(v) => { /* was: else-branch */ }")
                lines.append("// }")
            lines.append("// --- original if (%s) ---" % raw_cond)
            for l in sub_block(block, ln + 1):
                lines.append("//   " + l)
            for kind, ccond, cblock, cline in chain:
                lines.append("// --- original %s ---"
                             % ("else" if kind == "else" else
                                "else if (%s)" % ccond))
                for l in sub_block(cblock, cline + 1):
                    lines.append("//   " + l)
            return lines
        lines.append("if %s {" % cond)
        lines.extend("  " + l for l in sub_block(block, ln + 1))
        for kind, ccond, cblock, cline in chain:
            if kind == "elseif":
                lines.append("} else if %s {"
                             % java_expr(ccond, warn, cline, list_vars,
                                         static_classes, folded))
            else:
                lines.append("} else {")
            lines.extend("  " + l for l in sub_block(cblock, cline + 1))
        lines.append("}")
        return lines

    # while
    if re.match(r"while\s*\(", header):
        cond = _translate_cond(header, warn, ln, list_vars, static_classes)
        lines.append("while %s {" % cond)
        lines.extend("  " + l for l in sub_block(block, ln + 1))
        lines.append("}")
        return lines

    # do { } while (c);
    if header == "do":
        dcond = chain[0][1] if chain and chain[0][0] == "dowhile" else "true"
        warn.add(ln, "'do/while' rewritten as 'while true' with a trailing "
                     "break — verify the condition")
        lines.append("while true {")
        lines.extend("  " + l for l in sub_block(block, ln + 1))
        lines.append("  if not (%s) { break }"
                     % java_expr(dcond, warn, ln, list_vars,
                                 static_classes, folded))
        lines.append("}")
        return lines

    # for: enhanced or classic
    if re.match(r"for\s*\(", header):
        p = header.find("(")
        q = _find_matching(header, p, "(", ")")
        parts = _split_top_level(header[p + 1:q], ";")
        if len(parts) == 1:
            # enhanced for: [final] Type x : xs
            em = re.match(r"(?:final\s+)?([\w<>\[\].]+)\s+(\w+)\s*:\s*(.*)$",
                          parts[0].strip(), re.S)
            if em:
                et = java_type(em.group(1), warn, ln)
                var, xs = em.group(2), em.group(3).strip()
                if et.startswith("List["):
                    list_vars.add(var)
                lines.append("for %s in %s {"
                             % (var, ex(xs)))
                lines.extend("  " + l for l in sub_block(block, ln + 1))
                lines.append("}")
                return lines
            warn.add(ln, "could not parse for-loop header '%s' — dropped"
                     % header)
            lines.extend(_unconverted(header))
            return lines
        # classic for (init; cond; update)
        init_s, cond_s, upd_s = (parts + ["", "", ""])[:3]
        init_lines = []
        if init_s.strip():
            fake = {"kind": "simple", "text": init_s.strip(), "line": ln}
            init_lines = java_stmt(fake, warn, set(list_vars),
                                   static_classes, in_result_fn, folded)
        cond_n = ex(cond_s) if cond_s.strip() else "true"
        if not cond_s.strip():
            warn.add(ln, "for(;;) -> 'while true' — verify termination")
        upd_lines = []
        if upd_s.strip():
            fake = {"kind": "simple", "text": upd_s.strip(), "line": ln}
            upd_lines = java_stmt(fake, warn, set(list_vars),
                                  static_classes, in_result_fn, folded)
        warn.add(ln, "C-style for -> while loop (init/cond/update made "
                     "explicit) — verify bounds")
        lines.append("// for (%s; %s; %s)" % (init_s.strip(),
                                              cond_s.strip(), upd_s.strip()))
        lines.extend(init_lines)
        lines.append("while %s {" % cond_n)
        lines.extend("  " + l for l in sub_block(block, ln + 1))
        lines.extend("  " + l for l in upd_lines)
        lines.append("}")
        return lines

    # switch -> if/else-if chain on ==
    if re.match(r"switch\s*\(", header):
        warn.add(ln, "'switch' -> if/else-if chain on '=='; fall-through "
                     "cases flagged — verify")
        disc = _translate_cond(header, warn, ln, list_vars, static_classes)
        cases = _split_switch_cases(block, ln, warn)
        first = True
        for labels, stmts_text, cline in cases:
            conds = []
            for lab in labels:
                if lab == "default":
                    continue
                conds.append("%s == %s" % (disc, ex(lab)))
            body_lines = java_block(stmts_text, warn, cline,
                                    set(list_vars), static_classes,
                                    in_result_fn, folded)
            # strip trailing break/continue (chain structure replaces it)
            while body_lines and body_lines[-1].strip() in ("break",
                                                            "continue"):
                body_lines.pop()
            if not conds:  # default
                kw = "} else {" if not first else "{"
                if first:
                    warn.add(ln, "switch on %s has only a default — "
                                 "emitted as a bare block" % disc)
                    lines.append("{")
                else:
                    lines.append("} else {")
            else:
                kw = "if %s {" % " or ".join(conds) if first \
                    else "} else if %s {" % " or ".join(conds)
                lines.append(kw)
            lines.extend("  " + l for l in body_lines)
            first = False
        lines.append("}")
        return lines

    # try / catch / finally
    if header == "try":
        warn.add(ln, "'try/catch' -> Result[_, String] + '?' + match "
                     "(migration-guide.md Recipe 2); the try body is kept, "
                     "handlers are preserved as comments — rewrite by hand")
        lines.append("// WARNING: try/catch boundary — no exceptions in "
                     "Nova")
        lines.extend(sub_block(block, ln + 1))
        for kind, decl, cblock, cline in chain:
            if kind == "catch":
                ename = decl.split()[-1] if decl else "e"
                lines.append("// catch (%s) — handler not converted:"
                             % (decl or ""))
                for l in sub_block(cblock, cline + 1):
                    lines.append("//   " + l)
                lines.append("// (was bound as '%s')" % ename)
            elif kind == "finally":
                warn.add(cline, "'finally' has no Nova equivalent — "
                                "preserved as comment")
                lines.append("// finally — not converted:")
                for l in sub_block(cblock, cline + 1):
                    lines.append("//   " + l)
        return lines

    # synchronized -> body inline + warning
    if re.match(r"synchronized\s*\(", header):
        warn.add(ln, "'synchronized' dropped — Nova is single-threaded by "
                     "default; use spawn/structured tasks if this guarded "
                     "shared state")
        lines.append("// synchronized block — lock dropped:")
        lines.extend(sub_block(block, ln + 1))
        return lines

    warn.add(ln, "unrecognized block '%s' — kept as comment"
             % header[:60])
    lines.extend(_unconverted(header))
    for l in sub_block(block, ln + 1):
        lines.append("//   " + l)
    return lines


def _looks_like_decl(dm, rest):
    """Heuristic: is 'Type name [= ...]' a local declaration?"""
    jt = dm.group(1).strip()
    # a type rarely contains these; a call/assignment usually does
    if re.search(r"[+\-*/%<>=!&|]", jt):
        return False
    if re.match(r"(return|if|while|for|switch|throw|new)\b", jt):
        return False
    # must have at least a type-looking first token
    first = jt.split()[0] if jt.split() else ""
    if not re.match(r"[\w<>\[\].?]+$", first):
        return False
    # reject things like 'foo.bar(x)' (dots+parens in the declarator part)
    decls = dm.group(2)
    if "(" in jt:
        return False
    return True


def _split_switch_cases(block, base_line, warn):
    """Split a switch block into [(labels, body_text, line)]."""
    inner = _inner(block)
    src = _Src(inner, base_line + 1)
    cases = []
    labels, buf = [], []
    cur_line = src.line(0)
    i, n = 0, len(inner)
    while i < n:
        m = re.match(r"(case\s+[^:]+|default)\s*:", inner[i:])
        # make sure we're at depth 0
        if m and inner.count("{", 0, i) == inner.count("}", 0, i):
            new_labels = [l.strip() for l in m.group(1)[4:].split(",")] \
                if m.group(1) != "default" else ["default"]
            if "".join(buf).strip():
                # statements since the last label: close the previous case
                cases.append((labels, "".join(buf), cur_line))
                labels = new_labels
            else:
                # stacked 'case 1: case 2:' — merge the labels
                labels = labels + new_labels
            cur_line = src.line(i)
            buf = []
            i += m.end()
            continue
        buf.append(inner[i])
        i += 1
    if labels or "".join(buf).strip():
        cases.append((labels, "".join(buf), cur_line))
    # detect fall-through: a non-final case not ending in
    # break/return/continue/throw
    for labels, body_text, cline in cases[:-1]:
        stmts = [s for s in _split_top(body_text, cline)
                 if s["text"].strip() or s["kind"] == "compound"]
        if stmts:
            last = stmts[-1]
            tail = (last["text"] if last["kind"] == "simple"
                    else last["header"]).strip()
            if not re.match(r"(break|continue|return|throw)\b", tail):
                warn.add(cline, "switch fall-through in case %s — the "
                                 "if/else chain does NOT fall through; "
                                 "verify" % ", ".join(labels))
    return cases

# --- class-level migration ------------------------------------------------

def _split_class_members(body):
    """Split a class body into members: ("field", text) or
    ("member", annotations, header, block)."""
    members = []
    i, n = 0, len(body)
    buf = []
    in_str = None
    while i < n:
        ch = body[i]
        if in_str:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(body[i + 1])
                i += 1
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "{":
            close = _find_matching(body, i)
            header = "".join(buf).strip()
            block = body[i:close + 1] if close != -1 else body[i:]
            anns, header = _strip_annotations(header)
            members.append(("member", anns, header, block))
            buf = []
            i = (close + 1) if close != -1 else n
            continue
        if ch == ";":
            text = "".join(buf).strip()
            anns, text = _strip_annotations(text)
            if text:
                members.append(("field", anns, text))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        anns, tail = _strip_annotations(tail)
        members.append(("field", anns, tail))
    return members


def _parse_params(psrc, warn, line_no):
    """'Type a, final Type b' -> [(a, NovaType), ...]."""
    params = []
    if not psrc.strip():
        return params
    for p in _split_top_level(psrc, ","):
        p = p.strip()
        if not p:
            continue
        p = re.sub(r"^(final\s+)", "", p)
        # varargs: Type... name -> List[Type]
        m = re.match(r"([\w<>\[\].,\s\?]+?)\s*\.\.\.\s*(\w+)$", p)
        if m:
            nt = java_type(m.group(1), warn, line_no)
            params.append((m.group(2), "List[%s]" % nt))
            continue
        m = re.match(r"([\w<>\[\].,\s\?]+?)\s+(\w+)$", p)
        if not m:
            warn.add(line_no, "could not parse parameter '%s' — dropped"
                     % p)
            continue
        params.append((m.group(2), java_type(m.group(1), warn, line_no)))
    return params


def _accessor_field(name, fields):
    """Map getName/isName/setName to a field name, or None."""
    cand = None
    if name.startswith("get") and len(name) > 3 and name[3].isupper():
        cand = name[3].lower() + name[4:]
    elif name.startswith("is") and len(name) > 2 and name[2].isupper():
        cand = name[2].lower() + name[3:]
    elif name.startswith("set") and len(name) > 3 and name[3].isupper():
        cand = name[3].lower() + name[4:]
    if cand is None:
        return None
    for fname, _, _, _ in fields:
        if fname == cand or fname.lower() == cand.lower():
            return fname
    return None


def migrate_java_class(class_name, kind, header_rest, body, warn, base_line,
                       static_classes):
    """Migrate one class/interface; returns Nova lines."""
    lines = []
    src = _Src(body, base_line)

    # ---- header: extends / implements ----
    ext = imp = None
    m = re.search(r"\bextends\s+([\w.]+(?:<[^<>]*>)?)", header_rest)
    if m:
        ext = m.group(1)
        if "<" in ext:
            warn.add(base_line, "'extends %s' — generics erased; verify "
                                "the parent type" % ext)
            ext = ext.split("<")[0]
        ext = ext.split(".")[-1]
    m = re.search(r"\bimplements\s+([\w.,\s<>]+)", header_rest)
    if m:
        imp = []
        for part in _split_top_level(m.group(1), ","):
            part = part.strip().split("<")[0].split(".")[-1]
            if part:
                if "<" in m.group(1):
                    pass
                imp.append(part)
        if "<" in m.group(1):
            warn.add(base_line, "'implements' generics erased — verify")

    def _iface_sig(sig_text, sig_line):
        mm = re.match(r"(?:[\w\s]+\s+)?([\w<>\[\].]+)\s+(\w+)\s*\(([^)]*)\)"
                      r"\s*(throws\s+[\w,.\s]+)?$", sig_text.strip())
        if not mm:
            return None
        if mm.group(4):
            warn.add(sig_line, "interface method 'throws' flattened to "
                               "Result — pick an error type")
        ps = _parse_params(mm.group(3), warn, sig_line)
        ret = java_type(mm.group(1), warn, sig_line)
        ret_s = " -> %s" % ret if ret else ""
        return "  fn %s(%s)%s" % (mm.group(2),
                                 ", ".join("%s: %s" % p for p in ps),
                                 ret_s)

    if kind == "interface":
        lines.append("interface %s {" % class_name)
        for mem in _split_class_members(body):
            if mem[0] == "field":
                _, anns, text = mem
                sig = _iface_sig(text, base_line)
                if sig is not None:
                    # method declaration without a body
                    lines.append(sig)
                    continue
                warn.add(src.line(0), "interface constant '%s' — Nova "
                                      "interfaces carry no state; hoist to "
                                      "a module-level let by hand" % text)
                lines.append("  // UNCONVERTED (interface field): %s;"
                             % text)
                continue
            _, anns, mheader, block = mem
            sig = _iface_sig(mheader, base_line)
            if sig is not None:
                if "default" in mheader.split():
                    warn.add(base_line, "'default' method '%s' — Nova "
                                        "interfaces carry no "
                                        "implementation; move to an impl "
                                        "class" % mheader[:40])
                lines.append(sig)
            else:
                warn.add(base_line, "interface member not converted: '%s'"
                         % mheader[:60])
                lines.append("  // UNCONVERTED: %s" % mheader)
        lines.append("}")
        return lines, [], []

    # ---- collect members ----
    fields = []        # (name, nova_type, is_final, init_src)
    static_fields = []
    methods = []       # _JavaMethod
    static_methods = []
    ctors = []
    init_extra = []    # instance-initializer statements

    # record components live in the header: record Point(double x, ...)
    if kind == "record":
        rm = re.match(r"\s*\(([^)]*)\)", header_rest)
        if rm:
            for pname, ptype in _parse_params(rm.group(1), warn,
                                              base_line):
                fields.append((pname, ptype, True, None))
            warn.add(base_line, "'record %s' -> data class (compact "
                                "constructor logic, if any, dropped)"
                     % class_name)

    for mem in _split_class_members(body):
        if mem[0] == "field":
            _, anns, text = mem
            for a in anns:
                warn.add(base_line, "annotation '%s' on field dropped" % a)
            mods, rest = _strip_modifiers(text)
            fm = re.match(r"([\w<>\[\].,\s\?]+?)\s+(\w+)\s*(=\s*(.*))?$",
                          rest, re.S)
            if not fm:
                warn.add(base_line, "could not parse field '%s' — dropped"
                         % text[:60])
                continue
            jt, fname = fm.group(1).strip(), fm.group(2)
            init = fm.group(4)
            if "=" in jt:  # malformed
                warn.add(base_line, "could not parse field '%s' — dropped"
                         % text[:60])
                continue
            nt = java_type(jt, warn, base_line)
            is_final = "final" in mods
            if "static" in mods:
                static_fields.append((fname, nt, init, base_line))
            else:
                fields.append((fname, nt, is_final, init))
            continue
        # member with a block
        _, anns, mheader, block = mem
        for a in anns:
            if a != "@Override":
                warn.add(base_line, "annotation '%s' dropped — no Nova "
                                    "equivalent" % a)
        if not mheader.strip():
            # instance initializer { ... }
            warn.add(base_line, "instance initializer block moved into "
                                "init() — verify ordering")
            init_extra.append(block)
            continue
        mods, rest = _strip_modifiers(mheader)
        # constructor?
        cm = re.match(r"(%s)\s*\(([^)]*)\)\s*(throws\s+([\w,\s]+))?$"
                      % re.escape(class_name), rest)
        if cm:
            c = _JavaMethod()
            c.is_ctor = True
            c.name = class_name
            c.params = _parse_params(cm.group(2), warn, base_line)
            c.body = block
            c.line = base_line
            c.mods = mods
            c.throws = cm.group(4)
            ctors.append(c)
            continue
        if re.match(r"(class|interface|enum|record)\s+\w+", rest):
            # nested type: hoisted to top level by migrate_java
            lines.append("// (nested %s — emitted at top level)"
                         % rest.strip()[:60])
            continue
        mm = re.match(r"([\w<>\[\].,\s\?]+?)\s+(\w+)\s*\(([^)]*)\)\s*"
                      r"(throws\s+([\w,.\s]+))?$", rest)
        if not mm:
            warn.add(base_line, "member not recognized — kept as comment: "
                                "'%s'" % mheader[:60])
            lines.extend(_unconverted(mheader, label="UNCONVERTED member"))
            continue
        jm = _JavaMethod()
        jm.name = mm.group(2)
        jm.params = _parse_params(mm.group(3), warn, base_line)
        jm.ret = java_type(mm.group(1), warn, base_line)
        jm.body = block
        jm.line = base_line
        jm.mods = mods
        jm.annotations = anns
        jm.throws = mm.group(5)
        if "static" in mods:
            static_methods.append(jm)
        else:
            methods.append(jm)

    # ---- fold getters / setters / boilerplate ----
    folded = {}          # method name -> field name (for call rewriting)
    real_methods = []
    has_setter = False
    for jm in methods:
        acc_field = _accessor_field(jm.name, fields)
        is_getter = (jm.name.startswith("get") or jm.name.startswith("is")) \
            and not jm.params and acc_field
        is_setter = jm.name.startswith("set") and len(jm.params) == 1 \
            and acc_field and jm.ret is None
        if is_getter or is_setter:
            folded[jm.name] = acc_field
            warn.add(jm.line, "'%s' folded into field '%s' — calls "
                              "rewritten to field access" % (jm.name,
                                                             acc_field))
            if is_setter:
                has_setter = True
            continue
        if jm.name in ("equals", "hashCode", "toString"):
            folded.add(jm.name)
            warn.add(jm.line, "'%s' folded away — data class / class "
                              "equality covers it" % jm.name)
            continue
        real_methods.append(jm)

    # ---- data class or regular class? ----
    # A class with only static members is a static holder -> it becomes
    # nothing but hoisted module-level lets/fns (no class emitted).
    if not fields and not real_methods and not ctors and \
            (static_fields or static_methods):
        return [], static_fields, static_methods
    use_data_class = (not real_methods and not has_setter
                      and len(ctors) <= 1)
    if ctors and not use_data_class and len(ctors) > 1:
        warn.add(ctors[1].line, "multiple constructors — Nova has no "
                                "overloading; only the first is converted")
    if ctors and use_data_class:
        # constructor logic beyond field assignment is dropped
        cbody = _inner(ctors[0].body)
        assigns = set()
        for st in _split_top(cbody, base_line):
            if st["kind"] == "simple":
                m = re.match(r"(?:this\.)?(\w+)\s*=\s*(\w+)\s*$",
                             st["text"].strip())
                if m:
                    assigns.add((m.group(1), m.group(2)))
                    continue
            warn.add(st["line"], "constructor logic beyond field assignment "
                                 "dropped in data-class mapping — verify")
            break

    suffix = ""
    if ext:
        suffix += " extends %s" % ext
    if imp:
        suffix += " implements %s" % ", ".join(imp)

    if use_data_class:
        params = ", ".join("%s: %s" % (n, t) for n, t, _, _ in fields)
        lines.append("// Java bean -> data class (getters/setters folded)")
        lines.append("data class %s(%s)%s" % (class_name, params, suffix))
        for fname, nt, is_final, init in fields:
            if init:
                warn.add(base_line, "field initializer on '%s' dropped — "
                                    "data class fields come from constructor "
                                    "args" % fname)
        return lines, [], []

    # ---- regular class ----
    lines.append("class %s%s {" % (class_name, suffix))
    list_field_names = set()
    for fname, nt, is_final, init in fields:
        kw = "let" if is_final else "var"
        lines.append("  %s %s: %s" % (kw, fname, nt))
        if nt.startswith("List["):
            list_field_names.add(fname)
    # field initializers + instance initializers move into init
    field_inits = []
    for fname, nt, is_final, init in fields:
        if init:
            field_inits.append(
                (base_line,
                 "this.%s = %s" % (fname, java_expr(init, warn, base_line,
                                                   list_field_names,
                                                   static_classes,
                                                   folded))))
    # init from (first) constructor
    if ctors:
        c = ctors[0]
        if c.throws:
            warn.add(c.line, "constructor 'throws' — Nova init cannot fail "
                             "with Result; validate before constructing")
        ps = ", ".join("%s: %s" % p for p in c.params)
        lines.append("")
        lines.append("  init(%s) {" % ps)
        # super(...) must come first in Nova too
        cbody = _inner(c.body)
        cstmts = _split_top(cbody, base_line + 1)
        # hoist super() call
        rest_stmts = []
        for st in cstmts:
            t = (st["text"] if st["kind"] == "simple"
                 else st["header"]).strip()
            if re.match(r"super\s*\(", t):
                args = re.sub(r"^super\s*\(", "", t).rstrip(")").rstrip(";")
                lines.append("    super(%s)"
                             % java_expr(args, warn, st["line"],
                                         list_field_names, static_classes,
                                         folded))
            elif re.match(r"this\s*\(", t):
                warn.add(st["line"], "constructor chaining 'this(...)' — "
                                     "Nova init cannot delegate; inline the "
                                     "target constructor by hand")
                lines.append("    // UNCONVERTED: %s" % t)
            else:
                rest_stmts.append(st)
        for _, fi in field_inits:
            lines.append("    %s" % fi)
        for b in init_extra:
            for l in java_block(_inner(b), warn, base_line + 1,
                                set(list_field_names), static_classes,
                                False, folded):
                lines.append("    %s" % l)
        for st in rest_stmts:
            for l in java_stmt(st, warn, set(list_field_names),
                               static_classes, False, folded):
                lines.append("    %s" % l)
        lines.append("  }")
    elif field_inits or init_extra:
        lines.append("")
        lines.append("  init() {")
        for _, fi in field_inits:
            lines.append("    %s" % fi)
        for b in init_extra:
            for l in java_block(_inner(b), warn, base_line + 1,
                                set(list_field_names), static_classes,
                                False, folded):
                lines.append("    %s" % l)
        lines.append("  }")

    # methods
    for jm in real_methods:
        lines.append("")
        for l in _migrate_method(jm, warn, list_field_names,
                                 static_classes, class_name, folded):
            lines.append("  " + l if l.strip() else "")
    lines.append("}")
    return lines, static_fields, static_methods


def _migrate_method(jm, warn, list_vars, static_classes,
                    class_name="", folded=None):
    """A Java method -> Nova fn lines (without surrounding blank lines)."""
    lines = []
    override = "override " if "@Override" in jm.annotations else ""
    if "abstract" in jm.mods:
        warn.add(jm.line, "abstract method '%s' — Nova has no abstract; "
                          "emitted as a stub returning a zero value"
                 % jm.name)
    if "synchronized" in jm.mods:
        warn.add(jm.line, "method '%s' was synchronized — lock dropped "
                          "(see statement warning)" % jm.name)
    if "native" in jm.mods:
        warn.add(jm.line, "native method '%s' — no Nova equivalent; stub "
                          "emitted" % jm.name)
        lines.append("// UNCONVERTED (native): %s" % jm.name)
        return lines
    ps = ", ".join("%s: %s" % p for p in jm.params)
    in_result_fn = False
    if jm.throws:
        warn.add(jm.line, "checked exception '%s' flattened to "
                          "Result[_, String] — pick a real error type"
                 % jm.throws.strip())
        ret = jm.ret or "Unit"
        ret_s = " -> Result[%s, String]" % ret
        in_result_fn = True
    elif jm.ret:
        ret_s = " -> %s" % jm.ret
    else:
        ret_s = ""
    # effect guess: print in body -> effect (raw Java body still says
    # System.out.println at this point)
    eff = ""
    if re.search(r"\bprint\s*\(|System\.out\.println", jm.body):
        eff = "effect "
        warn.add(jm.line, "method '%s' marked 'effect' (calls print) — "
                          "review effect placement" % jm.name)
    lines.append("%s%sfn %s(%s)%s {" % (eff, override, jm.name, ps, ret_s))
    local_lists = set(list_vars)
    for pname, ptype in jm.params:
        if ptype.startswith("List["):
            local_lists.add(pname)
    for l in java_block(_inner(jm.body), warn, jm.line + 1, local_lists,
                        static_classes, in_result_fn, folded):
        lines.append("  " + l)
    lines.append("}")
    return lines


def _migrate_import(path, warn, line_no):
    """Map a Java import to a Nova import line (or None to drop)."""
    if path.startswith("java.lang."):
        return None  # auto-imported in Java; nothing to do
    if path == "java.util.List" or path.startswith("java.util."):
        return None  # Nova List/Option are built in
    if path.endswith(".*"):
        warn.add(line_no, "wildcard import '%s' — Nova imports are "
                          "explicit; import the modules you use" % path)
        return None
    mod = path.split(".")[-1]
    mod_nova = re.sub(r"(?<!^)(?=[A-Z])", "_", mod).lower()
    warn.add(line_no, "import '%s' -> 'import %s' — VERIFY the module "
                      "mapping by hand" % (path, mod_nova))
    return "import %s" % mod_nova


def migrate_java(source, filename):
    out = []
    warn = _Warnings()

    out.append("// Migrated from %s by `nova migrate --from java`" % filename)
    out.append("// EXPERIMENTAL — review every line, especially WARNINGs.")
    out.append("")

    src = _strip_comments(source)
    base = _Src(src, 1)

    # ---- imports ----
    nova_imports = []
    for m in re.finditer(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;",
                         src, re.M):
        line_no = base.line(m.start())
        if m.group(1):
            warn.add(line_no, "static import '%s' dropped — qualify uses "
                              "by hand" % m.group(2))
            continue
        imp = _migrate_import(m.group(2), warn, line_no)
        if imp and imp not in nova_imports:
            nova_imports.append(imp)
    out.extend(nova_imports)
    if nova_imports:
        out.append("")

    # ---- top-level types ----
    # find class/interface/enum/record declarations with brace matching
    decls = []
    for m in re.finditer(
            r"(?:^|(?<=\n))\s*((?:(?:public|private|protected|final|abstract|"
            r"static)\s+)*)(class|interface|enum|record)\s+(\w+)"
            r"([^\{;]*)\{", src):
        line_no = base.line(m.start())
        mods, kind, name, rest = (m.group(1), m.group(2), m.group(3),
                                  m.group(4))
        brace = m.end() - 1
        close = _find_matching(src, brace)
        if close == -1:
            warn.add(line_no, "unbalanced braces in '%s %s' — skipped"
                     % (kind, name))
            continue
        decls.append((mods, kind, name, rest, src[brace + 1:close],
                      line_no, m.start(), close))
    # also catch declarations indented inside nothing else (nested classes
    # are out of the mechanical subset)
    if not decls:
        warn.add(1, "no class/interface declaration found — nothing "
                    "converted")
        out.append("// (no convertible declarations found)")
        return "\n".join(out) + "\n", warn.items

    # nested declarations (regex-matched inside another class's braces)
    # are hoisted to top level rather than emitted twice
    outer, nested = [], []
    for d in sorted(decls, key=lambda d: d[6]):
        if any(o[6] < d[6] and d[7] <= o[7] for o in outer):
            warn.add(d[5], "nested %s '%s' hoisted to top level — "
                            "verify scoping" % (d[1], d[2]))
            nested.append(d)
        else:
            outer.append(d)
    decls = outer + nested

    static_classes = [d[2] for d in decls]
    module_fns = []
    for mods, kind, name, rest, body, line_no, _s, _e in decls:
        if kind == "enum":
            warn.add(line_no, "'enum %s' — Nova has no enums; model as a "
                              "data class + constants by hand" % name)
            out.append("// UNCONVERTED (enum): %s" % name)
            out.append("")
            continue
        if "abstract" in mods:
            warn.add(line_no, "abstract %s '%s' — Nova has no abstract; "
                              "emitted as a concrete %s, review" % (kind,
                                                                   name,
                                                                   kind))
        cls_lines, sfields, smethods = migrate_java_class(
            name, kind, rest, body, warn, line_no + 1, static_classes)
        if sfields or smethods:
            warn.add(line_no, "static members of '%s' hoisted to "
                              "module level" % name)
        for fname, nt, init, fline in sfields:
            init_s = (" = %s" % java_expr(init, warn, fline, set(),
                                          static_classes)) if init else ""
            module_fns.append("let %s: %s%s  // was %s.%s"
                              % (fname, nt, init_s, name, fname))
        for jm in smethods:
            for l in _migrate_method(jm, warn, set(), static_classes, name):
                module_fns.append(l)
            module_fns.append("")
        out.extend(cls_lines)
        out.append("")

    if module_fns:
        out.append("// ---- static members hoisted to module level ----")
        out.extend(module_fns)

    # ---- warnings summary (always in-file, so nothing lives only
    #      on stderr) ----
    out.append("")
    out.append("// ---- migrate warnings: %d (each points at a %s line) ----"
               % (len(warn), filename))
    for w in warn.items:
        out.append("// ! %s" % w)
    out.append("")
    return "\n".join(out) + "\n", warn.items

# ---------------------------------------------------------------------------
# JSX -> Nova
# ---------------------------------------------------------------------------

_JSX_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}


def _find_top_op(text, op):
    """Index of op at depth 0 (string-aware), -1 if none."""
    pd = bd = cd = 0
    in_str = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == in_str:
                in_str = None
        elif c in ("'", '"', "`"):
            in_str = c
        elif c == "(":
            pd += 1
        elif c == ")":
            pd -= 1
        elif c == "[":
            bd += 1
        elif c == "]":
            bd -= 1
        elif c == "{":
            cd += 1
        elif c == "}":
            cd -= 1
        elif (pd == 0 and bd == 0 and cd == 0
              and text.startswith(op, i)):
            return i
        i += 1
    return -1


def _string_spans(s):
    """(start, end) spans of string literals in s (for match guards)."""
    spans = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in ("'", '"', "`"):
            q = ch
            j = i + 1
            while j < n:
                if s[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if s[j] == q:
                    j += 1
                    break
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _in_spans(pos, spans):
    return any(a <= pos < b for a, b in spans)


def _rewrite_setters(s, setters, warn, line_no):
    """setX(arg) -> x = arg ; setX(p => expr) -> x = expr[p:=x]."""
    if not setters or "set" not in s:
        return s
    spans = _string_spans(s)
    for setter, state in setters.items():
        idx = 0
        while True:
            m = re.search(r"\b%s\s*\(" % re.escape(setter), s[idx:])
            if not m:
                break
            if _in_spans(idx + m.start(), spans):
                idx += m.end()
                continue
            p = idx + m.end() - 1
            args, close = _balanced_call_args(s, p)
            if args is None:
                break
            full = s[idx + m.start():close + 1]
            if len(args) == 1:
                a = args[0].strip()
                am = re.match(r"^\(?(\w+)\)?\s*=>\s*(.+)$", a, re.S)
                if am and am.group(2).strip().startswith("{"):
                    warn.add(line_no, "updater '%s' has a block body — "
                                      "inlined as a single expression; "
                                      "verify" % setter)
                if am:
                    param, body = am.group(1), am.group(2).strip()
                    if body.startswith("{") and body.endswith("}"):
                        body = body[1:-1].strip().rstrip(";")
                    body = re.sub(r"\b%s\b" % re.escape(param), state,
                                  body)
                    repl = "%s = %s" % (state, body)
                else:
                    # [...x, e] append pattern -> x.push(e)
                    sp = re.match(r"^\[\s*\.\.\.\s*(\w+)\s*,\s*(.+)\]$",
                                  a, re.S)
                    if sp and sp.group(1) == state:
                        repl = "%s.push(%s)" % (state,
                                               sp.group(2).strip())
                    else:
                        repl = "%s = %s" % (state, a)
            else:
                warn.add(line_no, "'%s' called with %d args — kept as "
                                  "assignment of the first" % (setter,
                                                               len(args)))
                repl = "%s = %s" % (state, args[0].strip())
            s = s[:idx + m.start()] + repl + s[close + 1:]
            idx = idx + m.start() + len(repl)
    return s


def js_expr(e, warn, line_no, setters=None):
    """Translate a JS expression to Nova."""
    setters = setters or {}
    e = e.strip()
    if not e:
        return e
    # ternary first, on the raw text: _find_top_ternary is string-aware,
    # and splitting into segments first would separate '?' from ':'
    qi, ci = _find_top_ternary(e)
    if qi != -1 and ci != -1:
        return "if %s { %s } else { %s }" % (
            js_expr(e[:qi].strip(), warn, line_no, setters),
            js_expr(e[qi + 1:ci].strip(), warn, line_no, setters),
            js_expr(e[ci + 1:].strip(), warn, line_no, setters))
    # setter rewriting next, on the raw text: _balanced_call_args is
    # string-aware, and splitting into segments first would break calls
    # whose arguments contain strings.
    e = _rewrite_setters(e, setters, warn, line_no)
    segs = _protect_strings(e)
    out = []
    for is_str, seg in segs:
        if not is_str:
            out.append(_js_expr_code(seg, warn, line_no, setters))
            continue
        # string literal conversions
        if seg.startswith("`") and seg.endswith("`") and len(seg) >= 2:
            out.append(_js_template(seg[1:-1], warn, line_no, setters))
        elif seg.startswith("'"):
            inner = seg[1:-1].replace("\\'", "'").replace('"', '\\"')
            out.append('"%s"' % inner)
        else:
            out.append(seg)
    return "".join(out).strip()


def _js_template(inner, warn, line_no, setters):
    """`a${b}c` -> "a" + str(b) + "c"."""
    parts = []
    i, buf = 0, []
    while i < len(inner):
        if inner.startswith("${", i):
            j = _find_matching(inner, i + 1, "{", "}")
            if j == -1:
                buf.append(inner[i:])
                break
            if buf:
                parts.append('"%s"' % "".join(buf).replace('"', '\\"'))
                buf = []
            parts.append("str(%s)" % js_expr(inner[i + 2:j], warn,
                                             line_no, setters))
            i = j + 1
        else:
            buf.append(inner[i])
            i += 1
    if buf:
        parts.append('"%s"' % "".join(buf).replace('"', '\\"'))
    return " + ".join(parts) if parts else '""'


def _js_expr_code(seg, warn, line_no, setters):
    s = seg
    s = s.replace("===", " == ").replace("!==", " != ")
    if re.search(r"\bnull\b|\bundefined\b", s):
        warn.add(line_no, "'null'/'undefined' -> None; the binding must be "
                          "Option[_] — verify")
    s = re.sub(r"\bnull\b|\bundefined\b", "None", s)
    if "?." in s:
        warn.add(line_no, "'?.' optional chaining -> plain '.'; model the "
                          "receiver as Option and match by hand")
        s = s.replace("?.", ".")
    qi = _find_top_op(s, "??")
    if qi != -1:
        warn.add(line_no, "'??' — right side dropped; use match on Option "
                          "by hand")
        s = s[:qi].strip()
    if re.search(r"\btypeof\b", s):
        warn.add(line_no, "'typeof' has no Nova equivalent — emitted as "
                          '"unknown"')
        s = re.sub(r"\btypeof\s+([\w.]+)", '"unknown"', s)
    if "console.log(" in s:
        s = s.replace("console.log(", "print(")
    if "JSON.stringify(" in s:
        warn.add(line_no, "JSON.stringify -> str(); Nova has no JSON "
                          "reflection — verify")
        s = s.replace("JSON.stringify(", "str(")
    if re.search(r"\bparseInt\s*\(", s):
        s = re.sub(r"\bparseInt\s*\(", "int(", s)
    if re.search(r"\bparseFloat\s*\(", s):
        s = re.sub(r"\bparseFloat\s*\(", "float(", s)
    if re.search(r"\bString\s*\(", s):
        s = re.sub(r"\bString\s*\(", "str(", s)
    if re.search(r"\bMath\.", s):
        warn.add(line_no, "'Math.*' kept as-is — verify a Nova equivalent")
    # .length -> len(x)
    s = re.sub(r"([\w\)\]]+(?:\.[\w\)\]]+)*)\.length\b",
               r"len(\1)", s)
    # await -> dropped with warning (semantics differ)
    if re.search(r"\bawait\b", s):
        warn.add(line_no, "'await' dropped — Nova's async model (async fn "
                          "+ spawn) differs from JS promises; rewrite by "
                          "hand")
        s = re.sub(r"\bawait\s+", "", s)
    # new X() -> X()
    if re.search(r"\bnew\s+\w+", s):
        warn.add(line_no, "'new X()' -> 'X()' — verify the constructor "
                          "mapping")
        s = re.sub(r"\bnew\s+(\w+)", r"\1", s)
    s = s.replace("&&", " and ")
    s = s.replace("||", " or ")
    s = re.sub(r"!(?!=)", "not ", s)
    if "..." in s:
        warn.add(line_no, "spread '...' has no Nova equivalent — dropped; "
                          "rewrite by hand")
        s = s.replace("...", "")
    # leftover arrows cannot be expressed (setX rewriting already ran in
    # js_expr on the raw text)
    if "=>" in s:
        warn.add(line_no, "arrow function '=>' — Nova has no first-class "
                          "functions; rewrite by hand")
    s = re.sub(r"[ \t]+", " ", s)
    return s


def _infer_state_type(init_src, warn, line_no, name):
    s = init_src.strip()
    if re.match(r"^-?\d+$", s):
        return "Int"
    if re.match(r"^-?\d+\.\d*([eE][-+]?\d+)?$", s):
        return "Float"
    if s in ("true", "false"):
        return "Bool"
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")) or \
       (s.startswith("`") and s.endswith("`")):
        return "String"
    if s == "[]":
        warn.add(line_no, "useState([]) — element type unknown; "
                          "List[Any] assumed, annotate by hand")
        return "List[Any]"
    if s == "{}" or s.startswith("{"):
        warn.add(line_no, "object-literal state '%s' — model as a data "
                          "class by hand; typed Any" % name)
        return "Any"
    warn.add(line_no, "useState initializer for '%s' is not a literal — "
                      "typed Any; annotate by hand" % name)
    return "Any"


# --- JSX parsing ----------------------------------------------------------

def _parse_jsx_seq(s, i, warn, line_no, stop_tag=None):
    """Parse JSX nodes starting at i; stop at </stop_tag>. Returns
    (nodes, new_i)."""
    nodes = []
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "<" and i + 1 < n and (s[i + 1].isalpha()
                                       or s[i + 1] == "/"):
            if s[i + 1] == "/":
                m = re.match(r"</\s*([\w.]+)\s*>", s[i:])
                tag = m.group(1) if m else "?"
                if m and stop_tag and tag == stop_tag:
                    return nodes, i + m.end()
                warn.add(line_no, "mismatched closing tag '</%s>' — "
                                  "skipped" % tag)
                i += m.end() if m else 2
                continue
            node, i = _parse_jsx_element(s, i, warn, line_no)
            nodes.append(node)
        elif ch == "{":
            j = _find_matching(s, i, "{", "}")
            if j == -1:
                warn.add(line_no, "unbalanced '{' in JSX — rest dropped")
                return nodes, n
            nodes.append({"kind": "expr", "text": s[i + 1:j].strip(),
                          "line": line_no})
            i = j + 1
        else:
            j = i
            while j < n:
                c = s[j]
                if c == "{":
                    break
                if c == "<" and j + 1 < n and (s[j + 1].isalpha()
                                              or s[j + 1] == "/"):
                    break
                j += 1
            text = s[i:j]
            if text.strip():
                nodes.append({"kind": "text",
                              "text": re.sub(r"\s+", " ", text).strip()})
            i = j
    if stop_tag:
        warn.add(line_no, "missing closing tag for <%s>" % stop_tag)
    return nodes, i


def _parse_jsx_element(s, i, warn, line_no):
    m = re.match(r"<([\w.]+)", s[i:])
    tag = m.group(1)
    i += m.end()
    n = len(s)
    attrs = []
    self_close = False
    while i < n:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if s.startswith("/>", i):
            self_close = True
            i += 2
            break
        if s[i] == ">":
            i += 1
            break
        if s[i] == "{":
            j = _find_matching(s, i, "{", "}")
            if j == -1:
                warn.add(line_no, "unbalanced '{' in attributes — "
                                  "rest dropped")
                i = n
                break
            attrs.append(("...spread", ("spread", s[i + 1:j].strip())))
            i = j + 1
            continue
        am = re.match(r"([-\w:]+)", s[i:])
        if not am:
            warn.add(line_no, "could not parse attribute near '%s' — "
                              "skipped" % s[i:i + 20])
            i += 1
            continue
        aname = am.group(1)
        i += am.end()
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i < n and s[i] == "=":
            i += 1
            while i < n and s[i] in " \t\r\n":
                i += 1
            if i < n and s[i] in ("'", '"'):
                q = s[i]
                j = i + 1
                while j < n and s[j] != q:
                    if s[j] == "\\":
                        j += 1
                    j += 1
                attrs.append((aname, ("str", s[i + 1:j])))
                i = j + 1
            elif i < n and s[i] == "{":
                j = _find_matching(s, i, "{", "}")
                if j == -1:
                    warn.add(line_no, "unbalanced '{' in attribute — "
                                      "attribute dropped")
                    i = n
                    break
                attrs.append((aname, ("expr", s[i + 1:j].strip())))
                i = j + 1
            else:
                bm = re.match(r"[^\s>/]+", s[i:])
                attrs.append((aname, ("str", bm.group(0))))
                i += bm.end()
        else:
            attrs.append((aname, ("bool", True)))
    node = {"kind": "element", "tag": tag, "attrs": attrs,
            "children": [], "line": line_no}
    if self_close or tag.split(".")[-1] in _JSX_VOID or tag == "Fragment" \
            or tag == "React.Fragment":
        return node, i
    children, i = _parse_jsx_seq(s, i, warn, line_no, stop_tag=tag)
    node["children"] = children
    return node, i


# --- view emission --------------------------------------------------------

def _emit_view(nodes, warn, setters, templates, indent):
    lines = []
    for node in nodes:
        k = node["kind"]
        if k == "text":
            if node["text"]:
                lines.append(indent + node["text"])
        elif k == "expr":
            lines.extend(_emit_view_expr(node["text"], warn, setters,
                                         templates,
                                         node.get("line", 0), indent))
        elif k == "element":
            lines.extend(_emit_element(node, warn, setters, templates,
                                       indent))
    return lines


def _if_open(cond, indent, warn, ln):
    """Open an <if> whose condition parses: bare identifiers use the
    documented `<if cond>` shape; anything else becomes `cond={expr}`
    with a warning."""
    c = cond.strip()
    if re.match(r"^not\s+\w+$", c):
        return "%s<if %s>" % (indent, c)
    if re.match(r"^\w+$", c):
        return "%s<if %s>" % (indent, c)
    warn.add(ln, "complex <if> condition '%s' — emitted as cond={...}; "
                 "the documented shape is <if name>" % c[:60])
    return "%s<if cond={%s}>" % (indent, c)


def _emit_view_expr(text, warn, setters, templates, line_no, indent):
    lines = []
    t = text.strip()
    if not t:
        return lines
    ln = line_no or 0

    # items.map(x => <jsx>)  ->  <for x in items>
    mi = t.find(".map(")
    if mi != -1:
        arr = t[:mi].strip()
        rest = t[mi + 5:]
        am = re.match(r"\s*\(?(\w+)\)?\s*=>\s*", rest)
        if am:
            var = am.group(1)
            jsx_part = rest[am.end():]
            if jsx_part.endswith(")"):
                jsx_part = jsx_part[:-1]
            jsx_part = jsx_part.strip()
            # parenthesized callback: p => (<div/>)
            if jsx_part.startswith("(") and jsx_part.endswith(")"):
                jsx_part = jsx_part[1:-1].strip()
            if jsx_part.startswith("<"):
                warn.add(ln, "'.map()' with a key= attribute drops the "
                              "key — Nova reconciles structurally")
                children, _ = _parse_jsx_seq(jsx_part, 0, warn, ln)
                lines.append("%s<for %s in %s>" % (
                    indent, var,
                    js_expr(arr, warn, ln, setters)))
                lines.extend(_emit_view(children, warn, setters,
                                        templates, indent + "  "))
                lines.append("%s</for>" % indent)
                return lines
            # NOTE: no // comment here — Nova's view parser has no comment
            # syntax; the drop lives in the warnings list + file summary.
            warn.add(ln, "'.map()' callback is not JSX — dropped (see "
                         "warnings summary)")
            return lines
        warn.add(ln, "'.map()' with a non-arrow callback — Nova has no "
                     "first-class functions; dropped (see warnings "
                     "summary)")
        return lines

    # cond ? <A/> : <B/>  ->  two <if> blocks
    qi, ci = _find_top_ternary(t)
    if qi != -1 and ci != -1:
        cond = t[:qi].strip()
        a = t[qi + 1:ci].strip()
        b = t[ci + 1:].strip()
        if a.startswith("<") and b.startswith("<"):
            warn.add(ln, "JSX ternary -> two <if> blocks; Nova <if> has "
                          "no else — verify")
            cond_n = js_expr(cond, warn, ln, setters)
            lines.append(_if_open(cond_n, indent, warn, ln))
            ca, _ = _parse_jsx_seq(a, 0, warn, ln)
            lines.extend(_emit_view(ca, warn, setters, templates,
                                    indent + "  "))
            lines.append("%s</if>" % indent)
            lines.append("%s// (ternary else-branch)" % indent)
            lines.append(_if_open("not (%s)" % cond_n, indent, warn, ln))
            cb, _ = _parse_jsx_seq(b, 0, warn, ln)
            lines.extend(_emit_view(cb, warn, setters, templates,
                                    indent + "  "))
            lines.append("%s</if>" % indent)
            return lines

    # cond && <jsx/>  ->  <if cond>
    ai = _find_top_op(t, "&&")
    if ai != -1:
        left, right = t[:ai].strip(), t[ai + 2:].strip()
        if right.startswith("<"):
            lines.append(_if_open(js_expr(left, warn, ln, setters),
                                  indent, warn, ln))
            cr, _ = _parse_jsx_seq(right, 0, warn, ln)
            lines.extend(_emit_view(cr, warn, setters, templates,
                                    indent + "  "))
            lines.append("%s</if>" % indent)
            return lines

    lines.append("%s{%s}" % (indent, js_expr(t, warn, ln, setters)))
    return lines


def _inline_template_call(h, warn, setters, templates, line_no):
    """Inline `name(args)` when name is a known handler template; returns
    the (possibly unchanged) expression."""
    m = re.match(r"^(\w+)\s*\(", h)
    if not m or m.group(1) not in templates:
        return h
    name = m.group(1)
    p = m.end() - 1
    args, close = _balanced_call_args(h, p)
    if args is None:
        return h
    rest = h[close + 1:].strip()
    if rest:
        return h  # not a simple call
    tparams, tbody, tline = templates[name]
    if len(args) != len(tparams):
        warn.add(line_no, "handler '%s' called with %d args but defined "
                          "with %d params — inlined anyway"
                 % (name, len(args), len(tparams)))
    body = tbody
    for tp, av in zip(tparams, args):
        body = re.sub(r"\b%s\b" % re.escape(tp), av.strip(), body)
    return body


def _translate_handler(hexpr, warn, setters, templates, line_no):
    """Translate an onXxx={...} value to a Nova handler expression, or
    None when it cannot be expressed (caller drops the attribute)."""
    h = hexpr.strip()

    def finish(body):
        body = body.strip()
        if body.startswith("{") and body.endswith("}"):
            inner = body[1:-1].strip()
            stmts = [s for s in _split_top(inner, line_no)
                     if s["text"].strip() or s["kind"] == "compound"]
            if len(stmts) != 1 or stmts[0]["kind"] != "simple":
                warn.add(line_no, "multi-statement event handler — Nova "
                                  "handlers are single expressions; "
                                  "extract to an effect fn by hand")
                return None
            body = stmts[0]["text"]
        if body.startswith("<"):
            warn.add(line_no, "event handler returns JSX — meaningless in "
                              "Nova; dropped")
            return None
        return js_expr(body, warn, line_no, setters)

    # 1. direct template call: onClick={addToCart(p)}
    inlined = _inline_template_call(h, warn, setters, templates, line_no)
    if inlined != h:
        return finish(inlined)
    # 2. arrow: (...) => body
    am = re.match(r"^\(?([\w\s,]*)\)?\s*=>\s*(.+)$", h, re.S)
    if am:
        params = [p.strip() for p in am.group(1).split(",") if p.strip()]
        body = am.group(2).strip()
        if params:
            warn.add(line_no, "event parameter(s) %s dropped — Nova "
                              "handlers receive no event object"
                     % ", ".join(params))
        body = _inline_template_call(body, warn, setters, templates,
                                     line_no)
        return finish(body)
    # 3. bare reference: handleClick or handleClick()
    bm = re.match(r"^(\w+)\s*(\(\s*\))?$", h)
    if bm:
        name = bm.group(1)
        if name in templates:
            tparams, tbody, tline = templates[name]
            if tparams:
                warn.add(line_no, "handler '%s' takes params %s — event "
                                  "object not available; inlined anyway"
                         % (name, ", ".join(tparams)))
            return finish(tbody)
        if name in setters:
            # onClick={setCount} — degenerate; warn
            warn.add(line_no, "bare setter reference '%s' — needs an "
                              "argument; dropped" % name)
            return None
        warn.add(line_no, "bare handler reference '%s' — emitted as a "
                          "call; verify" % name)
        return "%s()" % name
    # 4. setX(...) directly
    rw = _rewrite_setters(h, setters, warn, line_no)
    if rw != h:
        return js_expr(rw, warn, line_no, setters)
    warn.add(line_no, "event handler not recognized — dropped; extract to "
                      "an effect fn by hand")
    return None


def _emit_element(node, warn, setters, templates, indent):
    lines = []
    ln = node.get("line", 0)
    tag = node["tag"]
    if tag in ("Fragment", "React.Fragment"):
        return _emit_view(node["children"], warn, setters, templates,
                          indent)
    if tag and tag[0].isupper():
        warn.add(ln, "component usage '<%s>' passed through — verify Nova "
                      "component call syntax" % tag)
    attrs = []
    for aname, val in node["attrs"]:
        if aname == "key":
            continue  # warned at the .map() site
        if aname == "className":
            aname = "class"
        elif aname == "htmlFor":
            aname = "for"
        if aname == "style":
            warn.add(ln, "'style={{...}}' (CSS-in-JS) dropped — Nova "
                         "views have no inline styles")
            continue
        if aname == "dangerouslySetInnerHTML":
            warn.add(ln, "'dangerouslySetInnerHTML' dropped — no raw-HTML "
                         "escape hatch in Nova")
            continue
        if aname == "...spread":
            warn.add(ln, "spread attributes '{...%s}' dropped — list "
                         "props explicitly" % val[1][:30])
            continue
        if aname.startswith("on") and len(aname) > 2 and aname[2].isupper():
            if val[0] != "expr":
                warn.add(ln, "non-expression handler on '%s' — dropped"
                         % aname)
                continue
            h = _translate_handler(val[1], warn, setters, templates, ln)
            if h is None:
                continue
            if aname not in ("onClick", "onSubmit", "onChange", "onInput"):
                warn.add(ln, "event '%s' passed through — verify the Nova "
                             "event name" % aname)
            attrs.append('%s={%s}' % (aname, h))
            continue
        if val[0] == "bool":
            attrs.append(aname)
        elif val[0] == "str":
            attrs.append('%s="%s"' % (aname, val[1]))
        else:  # expr
            attrs.append("%s={%s}" % (aname, js_expr(val[1], warn, ln,
                                                    setters)))
    # NOTE: no // comment for the dropped handler — Nova's view parser
    # has no comment syntax; the drop lives in the warnings list + summary.
    attr_s = (" " + " ".join(attrs)) if attrs else ""
    if not node["children"]:
        lines.append("%s<%s%s/>" % (indent, tag, attr_s))
        return lines
    lines.append("%s<%s%s>" % (indent, tag, attr_s))
    lines.extend(_emit_view(node["children"], warn, setters, templates,
                            indent + "  "))
    lines.append("%s</%s>" % (indent, tag))
    return lines

# --- component processing -------------------------------------------------

def _mask_return_jsx(body, warn, base_line):
    """Replace unwrapped `return <jsx>;` with `return __JSX{n}__;`.

    The statement splitter treats `{` as a block start, which breaks on
    JSX expression braces; masking the JSX span first keeps splitting
    correct. Returns (masked_body, {key: jsx_text}).
    """
    masked = body
    masks = {}
    while True:
        m = re.search(r"\breturn\s*(<)", masked)
        if not m:
            break
        start = m.start(1)
        try:
            _, end = _parse_jsx_element(masked, start, warn, base_line)
        except Exception:
            break
        j = end
        while j < len(masked) and masked[j] in " \t\r\n":
            j += 1
        if j < len(masked) and masked[j] == ";":
            j += 1
        key = "__JSX%d__" % len(masks)
        masks[key] = masked[start:end]
        masked = masked[:m.start()] + "return " + key + ";" + masked[j:]
    return masked, masks


def _parse_props(params_src, warn, line_no, body_src):
    """Parse a component's parameter list -> [(name, NovaType)]. All JS
    props are untyped: Any + a warning each."""
    ps = params_src.strip()
    names = []
    if not ps:
        return []
    if ps.startswith("{"):
        close = _find_matching(ps, 0, "{", "}")
        inner = ps[1:close] if close != -1 else ps[1:]
        rest = ps[close + 1:].strip() if close != -1 else ""
        if rest.startswith(":"):
            warn.add(line_no, "TypeScript prop type '%s' dropped — "
                              "annotate Nova params by hand"
                     % rest[1:].strip()[:40])
        for p in _split_top_level(inner, ","):
            p = p.strip()
            if not p:
                continue
            if p.startswith("..."):
                warn.add(line_no, "rest props '%s' dropped — Nova has no "
                                  "spread params" % p)
                continue
            m = re.match(r"(\w+)\s*=\s*(.+)$", p, re.S)
            if m:
                warn.add(line_no, "default value for prop '%s' dropped — "
                                  "Nova has no default params" % m.group(1))
                names.append(m.group(1))
                continue
            m = re.match(r"(\w+)\s*:\s*(\w+)$", p)
            if m:
                warn.add(line_no, "prop rename '%s: %s' — Nova param is "
                                  "'%s'" % (m.group(1), m.group(2),
                                            m.group(2)))
                names.append(m.group(2))
                continue
            m = re.match(r"(\w+)$", p)
            if m:
                names.append(m.group(1))
            else:
                warn.add(line_no, "prop pattern '%s' not recognized — "
                                  "dropped" % p[:40])
    else:
        # (props) or (props: Props) — harvest props.X uses
        pname = re.split(r"\s*:\s*", ps)[0].strip()
        if ":" in ps:
            warn.add(line_no, "TypeScript prop type dropped — annotate "
                              "Nova params by hand")
        seen = []
        for m in re.finditer(r"\b%s\.(\w+)" % re.escape(pname), body_src):
            if m.group(1) not in seen:
                seen.append(m.group(1))
        if seen:
            warn.add(line_no, "props object '%s' destructured into params "
                              "%s — annotate types" % (pname,
                                                       ", ".join(seen)))
        else:
            warn.add(line_no, "props object '%s' unused or unrecognized — "
                              "no params emitted" % pname)
        names = seen
    params = []
    for n_ in names:
        warn.add(line_no, "prop '%s' is untyped (JS) — emitted as Any; "
                          "replace with a real Nova type" % n_)
        params.append((n_, "Any"))
    return params


def _process_component(name, params_src, body, warn, base_line):
    """Migrate one React function component -> Nova component lines."""
    lines = []
    params = _parse_props(params_src, warn, base_line, body)

    states = []       # (name, type, init_src, line)
    setters = {}      # setter name -> state name
    templates = {}    # name -> (params, body_src, line)
    inlines = {}      # name -> (expr_src, line)
    view_jsx = None
    view_line = 0

    masked_body, jsx_masks = _mask_return_jsx(body, warn, base_line)

    for st in _split_top(masked_body, base_line):
        ln = st["line"]
        if st["kind"] == "compound":
            warn.add(ln, "statement '%s' inside a component body — only "
                         "hooks, bindings and return are mechanical; "
                         "kept as comment" % st["header"][:40])
            lines.extend(_unconverted(st["header"], indent="  "))
            continue
        text = st["text"].strip().rstrip(";").strip()
        if not text:
            continue
        # const [x, setX] = useState(init)
        if "useState" in text:
            um = re.search(r"\buseState\s*\(", text)
            dm = re.match(r"(?:const|let)\s*\[\s*(\w+)\s*,\s*(\w+)\s*\]\s*=",
                          text)
            if um and dm:
                args, _ = _balanced_call_args(text, um.end() - 1)
                init = args[0].strip() if args else ""
                if re.match(r"^\s*\(?\)?\s*=>", init) or \
                   re.match(r"^\s*\(\s*\)\s*=>", init):
                    warn.add(ln, "lazy useState initializer — evaluated "
                                 "eagerly in Nova")
                    init = re.sub(r"^\s*\(?\)?\s*=>\s*", "", init)
                sname, setter = dm.group(1), dm.group(2)
                ntype = _infer_state_type(init, warn, ln, sname)
                states.append((sname, ntype, init, ln))
                setters[setter] = sname
                continue
            warn.add(ln, "'useState' used without array destructuring — "
                         "not converted")
            lines.extend(_unconverted(text, indent="  "))
            continue
        # hooks with no Nova equivalent
        hm = re.search(r"\b(useEffect|useRef|useMemo|useCallback|useContext|"
                       r"useReducer|useLayoutEffect|useId)\s*\(", text)
        if hm:
            hook = hm.group(1)
            if hook == "useEffect":
                warn.add(ln, "'useEffect' — no direct equivalent; extract "
                             "the effect into an `effect fn` and call it "
                             "from an event handler (cleanup functions "
                             "have no equivalent at all)")
            else:
                warn.add(ln, "'%s' — no Nova equivalent; state is "
                             "compiler-managed, memoization is "
                             "unnecessary" % hook)
            lines.extend(_unconverted(text, indent="  ", label="UNCONVERTED (%s)" % hook))
            continue
        # const name = (...) => ...  (handler template)
        tm = re.match(r"(?:const|let)\s+(\w+)\s*=\s*\(?([\w\s,]*)\)?\s*=>\s*"
                      r"(.+)$", text, re.S)
        if tm and "=>" in text:
            tparams = [p.strip() for p in tm.group(2).split(",")
                       if p.strip()]
            templates[tm.group(1)] = (tparams, tm.group(3).strip(), ln)
            continue
        # const name = function(...) {...}
        fm = re.match(r"(?:const|let)\s+(\w+)\s*=\s*function\s*"
                      r"\(([\w\s,]*)\)\s*(\{.*\})$", text, re.S)
        if fm:
            tparams = [p.strip() for p in fm.group(2).split(",")
                       if p.strip()]
            templates[fm.group(1)] = (tparams, fm.group(3).strip(), ln)
            continue
        # const x = expr / let x = expr  (inline binding)
        bm = re.match(r"(?:const|let)\s+(\w+)\s*=\s*(.+)$", text, re.S)
        if bm:
            inlines[bm.group(1)] = (bm.group(2).strip(), ln)
            continue
        # return <jsx>
        if text.startswith("return"):
            val = re.sub(r"^return\s*", "", text, count=1).strip()
            val = jsx_masks.get(val, val)  # unmask __JSXn__
            if val.startswith("(") and val.endswith(")"):
                val = val[1:-1]
            if view_jsx is not None:
                warn.add(ln, "multiple returns — Nova components render "
                             "once; only the last return is kept")
            view_jsx, view_line = val, ln
            continue
        # if (...) return ...  (early return)
        if re.match(r"if\s*\(", text):
            warn.add(ln, "conditional/early return before the main return "
                         "— Nova views are single-return; restructure "
                         "with <if> by hand")
            lines.extend(_unconverted(text, indent="  "))
            continue
        # expression statement (often setX(...) outside handlers)
        rw = _rewrite_setters(text, setters, warn, ln)
        if rw != text:
            lines.append("  // (was: %s)" % text[:70])
            lines.append("  %s" % js_expr(rw, warn, ln, setters))
            continue
        warn.add(ln, "statement not converted: '%s'" % text[:60])
        lines.append("  // UNCONVERTED: %s" % text[:80])

    if view_jsx is None:
        warn.add(base_line, "component '%s' has no return — empty view "
                            "emitted" % name)
        view_jsx = "<div></div>"

    # inline const bindings referenced in the view
    def inline_view(src_text):
        for iname, (iexpr, iline) in inlines.items():
            if re.search(r"\b%s\b" % re.escape(iname), src_text):
                warn.add(iline, "local '%s' inlined into the view — "
                                "verify" % iname)
                src_text = re.sub(
                    r"\{%s\}" % re.escape(iname),
                    "{%s}" % js_expr(iexpr, warn, iline, setters),
                    src_text)
                src_text = re.sub(
                    r"\b%s\b" % re.escape(iname),
                    "(%s)" % js_expr(iexpr, warn, iline, setters),
                    src_text)
        return src_text

    # props-object style: rewrite props.X -> X in the view
    if not params_src.strip().startswith("{"):
        pname = re.split(r"\s*:\s*", params_src.strip())[0].strip()
        if pname and re.match(r"\w+$", pname):
            view_jsx = re.sub(r"\b%s\.(\w+)" % re.escape(pname), r"\1",
                              view_jsx)
    view_src = inline_view(view_jsx)
    for iname, (iexpr, iline) in inlines.items():
        if not re.search(r"\b%s\b" % re.escape(iname), view_jsx):
            warn.add(iline, "local '%s' not used in the view — dropped; "
                            "hoist to a module fn if needed" % iname)

    nodes, _ = _parse_jsx_seq(view_src, 0, warn, view_line)
    # a component returning a bare expression (no JSX)
    if not nodes and view_src.strip():
        nodes = [{"kind": "expr", "text": view_src.strip(),
                  "line": view_line}]

    out = []
    out.append("component %s(%s) {" % (
        name, ", ".join("%s: %s" % p for p in params)))
    for sname, ntype, init, ln in states:
        init_n = js_expr(init, warn, ln, setters) if init else \
            _zero_value(ntype)
        out.append("  state %s: %s = %s" % (sname, ntype, init_n))
    if states:
        out.append("")
    # pass-through notes for dropped statements
    out.extend(lines)
    out.append("  view {")
    out.extend(_emit_view(nodes, warn, setters, templates, "    "))
    out.append("  }")
    out.append("}")
    return out


def _js_fn_body(body, warn, base_line, setters):
    """Translate a plain (non-component) JS function body to Nova lines."""
    lines = []
    for st in _split_top(body, base_line):
        ln = st["line"]
        if st["kind"] == "compound":
            header = st["header"].strip()
            block = st["block"]
            inner = _inner(block)
            if re.match(r"if\s*\(", header):
                cond = _translate_js_cond(header, warn, ln, setters)
                lines.append("if %s {" % cond)
                lines.extend("  " + l for l in
                             _js_fn_body(inner, warn, ln + 1, setters))
                for kind, ccond, cblock, cline in st.get("chain", []):
                    if kind == "elseif":
                        lines.append("} else if %s {" % _translate_js_cond(
                            "if (%s)" % ccond, warn, cline, setters))
                    else:
                        lines.append("} else {")
                    lines.extend("  " + l for l in
                                 _js_fn_body(_inner(cblock), warn,
                                             cline + 1, setters))
                lines.append("}")
            elif re.match(r"while\s*\(", header):
                lines.append("while %s {" % _translate_js_cond(
                    header, warn, ln, setters))
                lines.extend("  " + l for l in
                             _js_fn_body(inner, warn, ln + 1, setters))
                lines.append("}")
            elif re.match(r"for\s*\(", header):
                warn.add(ln, "for-loop kept mechanical — verify")
                lines.extend(_unconverted(header, label="UNCONVERTED loop"))
                for l in _js_fn_body(inner, warn, ln + 1, setters):
                    lines.append("//   " + l)
            else:
                warn.add(ln, "block '%s' not converted"
                         % header[:50])
                lines.extend(_unconverted(header))
            continue
        text = st["text"].strip().rstrip(";").strip()
        if not text:
            continue
        if "=>" in text and "set" not in text:
            warn.add(ln, "arrow function — Nova has no first-class "
                         "functions; dropped")
            lines.extend(_unconverted(text))
            continue
        m = re.match(r"return\s*(.*)$", text, re.S)
        if m:
            v = m.group(1).strip()
            lines.append("return %s" % js_expr(v, warn, ln, setters)
                         if v else "return")
            continue
        m = re.match(r"(?:const|let|var)\s+(\w+)\s*=\s*(.+)$", text, re.S)
        if m:
            kw = "var" if text.startswith("let") or text.startswith("var") \
                else "let"
            lines.append("%s %s = %s" % (kw, m.group(1),
                                        js_expr(m.group(2), warn, ln,
                                                setters)))
            continue
        rw = _rewrite_setters(text, setters, warn, ln)
        lines.append(js_expr(rw, warn, ln, setters))
    return lines


def _translate_js_cond(header, warn, ln, setters):
    p = header.find("(")
    q = _find_matching(header, p, "(", ")") if p != -1 else -1
    cond = header[p + 1:q] if q != -1 else ""
    return js_expr(cond, warn, ln, setters)


def _migrate_js_import(path, names, warn, line_no):
    """Map a JS import to a Nova import line (or None to drop)."""
    if path in ("react", "react-dom", "react-dom/client"):
        return None  # the framework itself; nothing to import in Nova
    if re.search(r"\.(css|scss|less|sass)$", path):
        warn.add(line_no, "stylesheet import '%s' dropped — Nova views "
                          "have no stylesheets" % path)
        return None
    if path.startswith("."):
        mod = path.split("/")[-1]
        mod = re.sub(r"\.(js|jsx|ts|tsx)$", "", mod)
        mod_nova = re.sub(r"(?<!^)(?=[A-Z])", "_", mod).lower()
        mod_nova = re.sub(r"\W", "_", mod_nova)
        if names:
            warn.add(line_no, "import '%s' -> 'import %s' — VERIFY the "
                              "module mapping by hand" % (path, mod_nova))
        return "import %s" % mod_nova
    warn.add(line_no, "external package '%s' has no Nova equivalent — "
                      "port it or find a Nova module" % path)
    return None


def migrate_jsx(source, filename):
    out = []
    warn = _Warnings()

    out.append("// Migrated from %s by `nova migrate --from jsx`" % filename)
    out.append("// EXPERIMENTAL — review every line, especially WARNINGs.")
    out.append("")

    src = _strip_comments(source, jsx_braces=True)
    base = _Src(src, 1)

    # ---- imports ----
    nova_imports = []
    for m in re.finditer(
            r"^\s*import\s+(?:(.+?)\s+from\s+)?['\"]([^'\"]+)['\"]",
            src, re.M):
        line_no = base.line(m.start())
        names, path = m.group(1), m.group(2)
        imp = _migrate_js_import(path, names, warn, line_no)
        if imp and imp not in nova_imports:
            nova_imports.append(imp)
    # require('x')
    for m in re.finditer(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""",
                         src):
        line_no = base.line(m.start())
        imp = _migrate_js_import(m.group(1), "require", warn, line_no)
        if imp and imp not in nova_imports:
            nova_imports.append(imp)
    out.extend(nova_imports)
    if nova_imports:
        out.append("")

    # ---- find components / functions ----
    found = []

    def body_of(m, open_paren_end):
        b = src.find("{", open_paren_end)
        if b == -1:
            return None, -1
        close = _find_matching(src, b)
        if close == -1:
            return None, -1
        return src[b:close + 1], close

    # spans of top-level function/component bodies, so nested arrows
    # (e.g. const handler = () => ... inside a component) are not
    # double-processed as module-level functions
    _spans = []

    for m in re.finditer(r"(export\s+default\s+|export\s+)?"
                         r"(async\s+)?function\s+(\w+)\s*\(", src):
        line_no = base.line(m.start())
        is_async = bool(m.group(2))
        name = m.group(3)
        p = m.end() - 1
        q = _find_matching(src, p, "(", ")")
        if q == -1:
            warn.add(line_no, "could not parse '%s' signature — skipped"
                     % name)
            continue
        params_src = src[p + 1:q]
        block, close = body_of(m, q)
        if block is None:
            warn.add(line_no, "could not find body of '%s' — skipped"
                     % name)
            continue
        _spans.append((m.start(), close))
        found.append((line_no, name, params_src, block, is_async,
                      "component" if name[:1].isupper() else "fn"))

    for m in re.finditer(r"(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*"
                         r"(async\s+)?(\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>",
                         src):
        line_no = base.line(m.start())
        name = m.group(1)
        # skip arrows nested inside an already-found function/component
        # body (e.g. const handler = () => ... inside a component)
        if any(s <= m.start() < e for s, e in _spans):
            continue
        # avoid double-matching `const X = () =>` handled above as fn
        if any(f[1] == name for f in found):
            continue
        is_async = bool(m.group(2))
        params_src = m.group(3)
        if params_src.startswith("("):
            params_src = params_src[1:-1]
        block, close = body_of(m, m.end())
        if block is None:
            # expression-bodied arrow: const C = () => <div/>;
            # capture until ';' at depth 0
            j = m.end()
            pd = bd = 0
            in_str = None
            expr_end = -1
            while j < len(src):
                ch = src[j]
                if in_str:
                    if ch == "\\":
                        j += 1
                    elif ch == in_str:
                        in_str = None
                elif ch in ("'", '"', "`"):
                    in_str = ch
                elif ch == "(":
                    pd += 1
                elif ch == ")":
                    pd -= 1
                elif ch == "[":
                    bd += 1
                elif ch == "]":
                    bd -= 1
                elif ch == ";" and pd == 0 and bd == 0:
                    expr_end = j
                    break
                j += 1
            if expr_end == -1:
                warn.add(line_no, "could not find body of '%s' — skipped"
                         % name)
                continue
            expr = src[m.end():expr_end].strip()
            block = "{ return %s; }" % expr
            close = expr_end
        _spans.append((m.start(), close))
        found.append((line_no, name, params_src, block, is_async,
                      "component" if name[:1].isupper() else "fn"))

    # drop export statements (the declarations above are converted)
    if not found:
        warn.add(1, "no function components found — nothing converted")
        out.append("// (no convertible declarations found)")

    for line_no, name, params_src, block, is_async, flavor in sorted(found):
        if is_async:
            warn.add(line_no, "'%s' is async — Nova's async model (async "
                              "fn + spawn discipline) differs from JS "
                              "promises; 'await' dropped, rewrite by hand"
                     % name)
        body = _inner(block)
        if flavor == "component":
            out.extend(_process_component(name, params_src, body, warn,
                                          line_no + 1))
        else:
            warn.add(line_no, "function '%s' is not a component "
                              "(lowercase) — emitted as a module fn; "
                              "params typed Any" % name)
            ps = []
            for p in _split_top_level(params_src, ","):
                p = p.strip()
                if not p:
                    continue
                pm = re.match(r"(\w+)", p)
                if pm:
                    ps.append((pm.group(1), "Any"))
            out.append("fn %s(%s) {" % (
                name, ", ".join("%s: %s" % p for p in ps)))
            out.extend("  " + l for l in
                       _js_fn_body(body, warn, line_no + 1, {}))
            out.append("}")
        out.append("")

    # ---- warnings summary ----
    out.append("// ---- migrate warnings: %d (each points at a %s line) ----"
               % (len(warn), filename))
    for w in warn.items:
        out.append("// ! %s" % w)
    out.append("")
    return "\n".join(out) + "\n", warn.items


def migrate(source, filename, flavor):
    if flavor == "java":
        return migrate_java(source, filename)
    if flavor == "jsx":
        return migrate_jsx(source, filename)
    raise ValueError("unknown flavor %r (want java|jsx)" % flavor)
