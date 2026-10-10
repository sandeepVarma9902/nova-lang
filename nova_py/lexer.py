# lexer.py — Nova tokenizer.
#
# Newlines are significant (statement separators) except inside () and [].
# JSX view blocks are scanned at the character level by the parser, so the
# lexer never needs a JSX mode.

KEYWORDS = {
    "let", "var", "fn", "effect", "class", "interface", "extends",
    "implements", "data", "init", "this", "super", "component", "state",
    "view", "import", "if", "else", "while", "for", "in", "match",
    "return", "requires", "ensures", "result", "and", "or", "not",
    "true", "false", "Some", "None", "Ok", "Err", "override", "ai",
    "spawn", "stream", "async", "spawn_async", "nursery",
    "context", "provide", "consume",
}

# Identifiers that are banned: Nova has no null. Using one is a
# dedicated, AI-friendly diagnostic (E_NO_NULL) instead of a mystery error.
BANNED = {"null", "nil", "undefined", "NoneType"}


class Token:
    __slots__ = ("kind", "value", "line", "col")

    def __init__(self, kind, value, line, col):
        self.kind = kind      # e.g. "IDENT", "INT", "+", "->", "NEWLINE", "EOF"
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return "Tok(%s, %r, %d:%d)" % (self.kind, self.value, self.line, self.col)


class LexError(Exception):
    def __init__(self, message, line, col):
        super().__init__(message)
        self.line = line
        self.col = col


class Lexer:
    def __init__(self, text, filename="<input>"):
        self.text = text
        self.filename = filename
        self.pos = 0
        self.line = 1
        self.col = 1
        self.depth = 0  # inside () and []

    # -- char helpers -------------------------------------------------
    def peek(self):
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def advance(self):
        ch = self.text[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    # -- main ---------------------------------------------------------
    def tokenize(self):
        toks = []
        while self.pos < len(self.text):
            ch = self.peek()
            if ch in " \t\r":
                self.advance()
            elif ch == "\n":
                self.advance()
                if self.depth == 0:
                    toks.append(Token("NEWLINE", "\n", self.line - 1, self.col))
            elif ch == "/" and self.text[self.pos + 1:self.pos + 2] == "/":
                while self.pos < len(self.text) and self.peek() != "\n":
                    self.advance()
            elif ch.isdigit():
                toks.append(self.lex_number())
            elif ch.isalpha() or ch == "_":
                toks.append(self.lex_ident())
            elif ch == '"':
                toks.append(self.lex_string())
            else:
                tok = self.lex_op()
                if tok:
                    toks.append(tok)
                    if tok.kind in ("(", "["):
                        self.depth += 1
                    elif tok.kind in (")", "]"):
                        self.depth -= 1
        toks.append(Token("EOF", "", self.line, self.col))
        return toks

    def lex_number(self):
        line, col = self.line, self.col
        start = self.pos
        while self.peek().isdigit():
            self.advance()
        if self.peek() == "." and self.text[self.pos + 1:self.pos + 2].isdigit():
            self.advance()
            while self.peek().isdigit():
                self.advance()
            return Token("FLOAT", float(self.text[start:self.pos]), line, col)
        return Token("INT", int(self.text[start:self.pos]), line, col)

    def lex_ident(self):
        line, col = self.line, self.col
        start = self.pos
        while self.peek().isalnum() or self.peek() == "_":
            self.advance()
        word = self.text[start:self.pos]
        if word in KEYWORDS:
            return Token(word, word, line, col)
        return Token("IDENT", word, line, col)

    def lex_string(self):
        line, col = self.line, self.col
        self.advance()  # opening quote
        out = []
        while True:
            ch = self.peek()
            if ch == "":
                raise LexError("unterminated string", line, col)
            if ch == '"':
                self.advance()
                break
            if ch == "\\":
                self.advance()
                esc = self.peek()
                mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
                if esc not in mapping:
                    raise LexError("bad escape '\\%s'" % esc, self.line, self.col)
                out.append(mapping[esc])
                self.advance()
            elif ch == "\n":
                raise LexError("unterminated string", line, col)
            else:
                out.append(ch)
                self.advance()
        return Token("STRING", "".join(out), line, col)

    def lex_op(self):
        line, col = self.line, self.col
        two = self.text[self.pos:self.pos + 2]
        if two in ("->", "=>", "==", "!=", "<=", ">="):
            self.advance()
            self.advance()
            return Token(two, two, line, col)
        ch = self.advance()
        if ch in "+-*/%?=:.(),;{}[]<>":
            return Token(ch, ch, line, col)
        raise LexError("unexpected character %r" % ch, line, col)
