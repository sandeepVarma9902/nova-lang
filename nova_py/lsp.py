# lsp.py — EXPERIMENTAL Nova Language Server Protocol skeleton.
#
# Serves Nova over stdio with JSON-RPC (LSP 3.x base protocol), stdlib only.
#
#   python3 -m nova_lang.lsp        # or `nova lsp` once the CLI wires it
#
# What is REAL: diagnostics come straight from nova's own static checker
# (check_source) — the same codes `nova check` emits. They are pushed to the
# client on open/change via textDocument/publishDiagnostics.
#
# What is HEURISTIC (see docs/v2-lsp.md): hover does NOT use the checker's
# scopes. It does a best-effort word-based lookup against a regex-scanned
# declaration index of the open document. Fine for "what's this variable",
# not a substitute for real semantic hover.
#
# Status: experimental skeleton. Not wired into cli.py yet; the `nova lsp`
# command is the parent's job.

import json
import os
import re
import sys
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

SERVER_NAME = "nova-lsp"
SERVER_VERSION = "0.1.0-experimental"

# LSP DiagnosticSeverity
SEVERITY_ERROR = 1
SEVERITY_WARNING = 2

# LSP TextDocumentSyncKind
SYNC_FULL = 1


def _log(msg):
    """Log to stderr — stdout is the wire, never write frames there."""
    sys.stderr.write("[nova-lsp] %s\n" % msg)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# JSON-RPC framing (Content-Length headers over stdio)
# ---------------------------------------------------------------------------

def read_message(stream):
    """Read one LSP message from a binary stream. Returns dict or None on EOF."""
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    try:
        length = int(headers.get(b"content-length", 0))
    except ValueError:
        _log("bad Content-Length header: %r" % (headers.get(b"content-length"),))
        return None
    body = stream.read(length) if length > 0 else b""
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        _log("bad JSON body: %s" % e)
        return None


def write_message(stream, msg):
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    stream.write(b"Content-Length: %d\r\n\r\n" % len(body))
    stream.write(body)
    stream.flush()


def response(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def error_response(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def notification(method, params):
    return {"jsonrpc": "2.0", "method": method, "params": params}


# ---------------------------------------------------------------------------
# URIs and documents
# ---------------------------------------------------------------------------

def uri_to_path(uri):
    """file:///a/b.nova -> /a/b.nova. Returns None for non-file URIs."""
    try:
        parts = urlparse(uri)
    except Exception:
        return None
    if parts.scheme != "file":
        return None
    return url2pathname(unquote(parts.path)) or None


class Document:
    def __init__(self, uri, text, version):
        self.uri = uri
        self.text = text
        self.version = version

    @property
    def lines(self):
        return self.text.splitlines()


# ---------------------------------------------------------------------------
# Diagnostics — straight from the real Nova checker
# ---------------------------------------------------------------------------

def _word_end(line_text, col0):
    """Extend a 0-based column over identifier chars for a saner range end."""
    i = col0
    n = len(line_text)
    while i < n and (line_text[i].isalnum() or line_text[i] == "_"):
        i += 1
    return max(i, col0 + 1)


def check_document(doc):
    """Run Nova's checker over the document; return LSP Diagnostics."""
    from .checker import check_source
    path = uri_to_path(doc.uri) or "untitled.nova"
    basedir = os.path.dirname(os.path.abspath(path))
    try:
        _, diags = check_source(doc.text, path, basedir, is_entry=True)
    except Exception as e:  # the checker should never blow up the server
        _log("check_source crashed: %r" % e)
        return [{
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": 0, "character": 1}},
            "severity": SEVERITY_ERROR,
            "code": "E_LSP_INTERNAL",
            "source": SERVER_NAME,
            "message": "nova-lsp internal error while checking: %s" % e,
        }]
    out = []
    lines = doc.lines
    for d in diags:
        line0 = max(0, d.line - 1)
        char0 = max(0, d.col - 1)
        line_text = lines[line0] if line0 < len(lines) else ""
        end_char = _word_end(line_text, char0)
        message = d.message
        if d.hint:
            message += "\nHint: " + d.hint
        out.append({
            "range": {"start": {"line": line0, "character": char0},
                      "end": {"line": line0, "character": end_char}},
            "severity": SEVERITY_ERROR,
            "code": d.code,
            "source": "nova",
            "message": message,
        })
    return out


# ---------------------------------------------------------------------------
# Hover — HEURISTIC. Regex-scanned declaration index, not checker scopes.
# ---------------------------------------------------------------------------

_FN_RE = re.compile(
    r"^\s*(?:(pure|effect|io|net|async(?:\s+(?:io|net))?)\s+)?"
    r"fn\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:->\s*([^{\s]+))?")
_LET_RE = re.compile(
    r"^\s*(let|var)\s+([A-Za-z_]\w*)\s*(?::\s*([A-Za-z_][\w\[\], ]*?))?\s*=")
_STATE_RE = re.compile(
    r"^\s*state\s+([A-Za-z_]\w*)\s*(?::\s*([A-Za-z_][\w\[\], ]*?))?\s*=")
_CLASS_RE = re.compile(r"^\s*(class|interface)\s+([A-Za-z_]\w*)")
_COMPONENT_RE = re.compile(r"^\s*component\s+([A-Za-z_]\w*)\s*(\([^)]*\))?")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def _infer_literal_type(rhs):
    rhs = rhs.strip()
    if re.fullmatch(r"-?\d+", rhs):
        return "Int"
    if re.fullmatch(r"-?\d+\.\d+", rhs):
        return "Float"
    if rhs in ("true", "false"):
        return "Bool"
    if (rhs.startswith('"') and rhs.endswith('"')) or \
       (rhs.startswith("'") and rhs.endswith("'")):
        return "String"
    return None


def build_symbol_index(text):
    """Scan the document for declarations. name -> dict(info)."""
    index = {}
    for lineno, line in enumerate(text.splitlines()):
        m = _FN_RE.match(line)
        if m:
            effect, name, params, ret = m.groups()
            sig = "fn %s(%s)" % (name, params.strip())
            if ret:
                sig += " -> " + ret.strip()
            index[name] = {
                "kind": "function",
                "signature": sig,
                "effect": (effect or "pure").strip(),
                "line": lineno + 1,
            }
            # index params too, so hovering a param shows its type
            for p in params.split(","):
                p = p.strip()
                pm = re.match(r"([A-Za-z_]\w*)\s*(?::\s*(.+))?$", p)
                if pm and pm.group(1):
                    index[pm.group(1)] = {
                        "kind": "parameter",
                        "signature": "%s: %s" % (pm.group(1),
                                                pm.group(2).strip()
                                                if pm.group(2) else "inferred"),
                        "line": lineno + 1,
                    }
            continue
        m = _LET_RE.match(line)
        if m:
            kind, name, typ = m.groups()
            rhs = line.split("=", 1)[1] if "=" in line else ""
            inferred = _infer_literal_type(rhs) if not typ else None
            index[name] = {
                "kind": "variable" if kind == "var" else "constant",
                "signature": "%s %s%s" % (
                    kind, name,
                    ": " + (typ.strip() if typ
                            else (inferred + " (inferred)" if inferred
                                  else "inferred"))),
                "mutable": kind == "var",
                "line": lineno + 1,
            }
            continue
        m = _STATE_RE.match(line)
        if m:
            name, typ = m.groups()
            index[name] = {
                "kind": "state",
                "signature": "state %s%s" % (
                    name, ": " + typ.strip() if typ else ""),
                "line": lineno + 1,
            }
            continue
        m = _CLASS_RE.match(line)
        if m:
            kind, name = m.groups()
            index[name] = {"kind": kind, "signature": "%s %s" % (kind, name),
                           "line": lineno + 1}
            continue
        m = _COMPONENT_RE.match(line)
        if m:
            name, params = m.groups()
            index[name] = {
                "kind": "component",
                "signature": "component %s%s" % (name, params or ""),
                "line": lineno + 1,
            }
            continue
    return index


def word_at_position(doc, line, character):
    lines = doc.lines
    if line < 0 or line >= len(lines):
        return None
    text = lines[line]
    if character < 0 or character > len(text):
        return None
    start = character
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    end = character
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    word = text[start:end]
    return word if _IDENT_RE.fullmatch(word) else None


def hover_for(doc, line, character):
    word = word_at_position(doc, line, character)
    if not word:
        return None
    info = build_symbol_index(doc.text).get(word)
    if not info:
        return None
    parts = ["```nova", info["signature"], "```"]
    detail = "%s · declared on line %d" % (info["kind"], info["line"])
    if info.get("effect"):
        detail += " · effect: `%s`" % info["effect"]
    if info.get("mutable"):
        detail += " · mutable"
    parts.append(detail)
    parts.append("_Heuristic hover — declaration scan, not type-checked._")
    return {"contents": {"kind": "markdown", "value": "\n".join(parts)}}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class LspServer:
    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout
        self.documents = {}          # uri -> Document
        self.shutdown_requested = False
        self.root_path = None
        self.handlers = {
            "initialize": self.on_initialize,
            "initialized": self.on_initialized,
            "shutdown": self.on_shutdown,
            "exit": self.on_exit,
            "textDocument/didOpen": self.on_did_open,
            "textDocument/didChange": self.on_did_change,
            "textDocument/didClose": self.on_did_close,
            "textDocument/hover": self.on_hover,
        }

    # -- transport ------------------------------------------------------
    def send(self, msg):
        write_message(self.stdout, msg)

    def publish_diagnostics(self, doc):
        diags = check_document(doc)
        self.send(notification("textDocument/publishDiagnostics", {
            "uri": doc.uri,
            "version": doc.version,
            "diagnostics": diags,
        }))

    # -- lifecycle ------------------------------------------------------
    def on_initialize(self, rid, params):
        params = params or {}
        root_uri = params.get("rootUri") or ""
        path = uri_to_path(root_uri)
        self.root_path = path or os.getcwd()
        return response(rid, {
            "capabilities": {
                "textDocumentSync": {
                    "openClose": True,
                    "change": SYNC_FULL,
                    "save": False,
                },
                "hoverProvider": True,
            },
            "serverInfo": {"name": SERVER_NAME,
                           "version": SERVER_VERSION},
        })

    def on_initialized(self, rid, params):
        return None  # notification

    def on_shutdown(self, rid, params):
        self.shutdown_requested = True
        return response(rid, None)

    def on_exit(self, rid, params):
        # exit is a notification; rid is None
        raise SystemExit(0 if self.shutdown_requested else 1)

    # -- documents ------------------------------------------------------
    def _apply_full_change(self, params):
        td = params["textDocument"]
        changes = params.get("contentChanges", [])
        text = changes[-1]["text"] if changes else ""
        return td["uri"], td.get("version"), text

    def on_did_open(self, rid, params):
        td = params["textDocument"]
        doc = Document(td["uri"], td["text"], td.get("version"))
        self.documents[doc.uri] = doc
        self.publish_diagnostics(doc)
        return None

    def on_did_change(self, rid, params):
        uri, version, text = self._apply_full_change(params)
        doc = Document(uri, text, version)
        self.documents[uri] = doc
        self.publish_diagnostics(doc)
        return None

    def on_did_close(self, rid, params):
        uri = params["textDocument"]["uri"]
        self.documents.pop(uri, None)
        return None

    def on_hover(self, rid, params):
        td = params["textDocument"]
        pos = params["position"]
        doc = self.documents.get(td["uri"])
        if doc is None:
            return response(rid, None)
        return response(rid, hover_for(doc, pos.get("line", 0),
                                      pos.get("character", 0)))

    # -- dispatch -------------------------------------------------------
    def handle(self, msg):
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params")
        handler = self.handlers.get(method)
        if handler is None:
            if rid is not None:
                self.send(error_response(rid, -32601,
                                         "Method not found: %s" % method))
            return
        try:
            reply = handler(rid, params)
        except SystemExit:
            raise
        except Exception as e:
            _log("handler %s crashed: %r" % (method, e))
            if rid is not None:
                self.send(error_response(rid, -32603,
                                         "Internal error: %s" % e))
            return
        if reply is not None:
            self.send(reply)

    def serve(self):
        while True:
            msg = read_message(self.stdin)
            if msg is None:
                _log("stdin closed, exiting")
                return
            self.handle(msg)


def main(argv=None):
    server = LspServer(sys.stdin.buffer, sys.stdout.buffer)
    _log("starting (experimental)")
    server.serve()


if __name__ == "__main__":
    main()
