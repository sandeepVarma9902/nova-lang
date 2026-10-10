#!/usr/bin/env python3
# test_lsp.py — smoke test for the EXPERIMENTAL nova LSP server.
#
# Spawns `python3 -m nova_lang.lsp` as a subprocess and speaks LSP over
# stdio: initialize -> didOpen (a file with a real type error) -> hover ->
# didChange (fix it) -> unknown method -> shutdown -> exit.
#
# Run: python3 tests/test_lsp.py   (from the nova-lang root)

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A Nova file with a real check error (mirrors tests/errors/not_send.nova).
BAD_SOURCE = """\
// ERROR: E_NOT_SEND
effect fn main() {
  var total = 0
  spawn {
    total = total + 1
  }
}
main()
"""

GOOD_SOURCE = """\
effect fn main() {
  let total = 41
  print(total + 1)
}
main()
"""


class LspClient:
    def __init__(self, proc):
        self.proc = proc
        self.next_id = 1
        self.pending = {}        # id -> response
        self.diagnostics = []    # publishDiagnostics notifications

    def send(self, msg):
        body = json.dumps(msg).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body))
        self.proc.stdin.write(body)
        self.proc.stdin.flush()

    def read_frame(self, timeout=10):
        self.proc.wait() if False else None
        import select
        fd = self.proc.stdout.fileno()
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            raise TimeoutError("timed out waiting for LSP frame")
        headers = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise EOFError("server closed stdout")
            line = line.strip()
            if not line:
                break
            name, _, value = line.partition(b":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers[b"content-length"])
        body = self.proc.stdout.read(length)
        return json.loads(body.decode("utf-8"))

    def dispatch(self, timeout=10):
        """Read frames until a pending response or a diagnostics push."""
        while True:
            msg = self.read_frame(timeout)
            if "id" in msg and ("result" in msg or "error" in msg):
                self.pending[msg["id"]] = msg
                return ("response", msg)
            if msg.get("method") == "textDocument/publishDiagnostics":
                self.diagnostics.append(msg["params"])
                return ("diagnostics", msg["params"])

    def request(self, method, params):
        rid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params})
        while rid not in self.pending:
            kind, _ = self.dispatch()
            if kind == "diagnostics":
                continue
        return self.pending.pop(rid)

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def wait_diagnostics(self, uri, timeout=10):
        while True:
            for p in self.diagnostics:
                if p["uri"] == uri:
                    self.diagnostics.remove(p)
                    return p["diagnostics"]
            kind, _ = self.dispatch(timeout)


failures = []


def check(name, cond, detail=""):
    if cond:
        print("ok   lsp/%s" % name)
    else:
        print("FAIL lsp/%s %s" % (name, detail))
        failures.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="nova-lsp-test-")
    path = os.path.join(tmp, "main.nova")
    uri = "file://" + path

    proc = subprocess.Popen(
        [sys.executable, "-m", "nova_lang.lsp"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    client = LspClient(proc)
    try:
        # --- initialize ------------------------------------------------
        resp = client.request("initialize", {
            "processId": None,
            "rootUri": "file://" + tmp,
            "capabilities": {},
        })
        result = resp.get("result", {})
        caps = result.get("capabilities", {})
        check("initialize/capabilities",
              caps.get("hoverProvider") is True
              and caps.get("textDocumentSync", {}).get("change") == 1,
              "caps=%r" % (caps,))
        check("initialize/serverInfo",
              result.get("serverInfo", {}).get("name") == "nova-lsp",
              "serverInfo=%r" % (result.get("serverInfo"),))
        client.notify("initialized", {})

        # --- didOpen with a real type error ---------------------------
        client.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "nova",
                             "version": 1, "text": BAD_SOURCE}})
        diags = client.wait_diagnostics(uri)
        codes = [d.get("code") for d in diags]
        check("didOpen/publishes-diagnostics",
              "E_NOT_SEND" in codes, "codes=%r" % (codes,))
        check("diagnostic/shape",
              all(d.get("severity") == 1
                  and d["range"]["start"]["line"] >= 0
                  and d["range"]["end"]["character"]
                  > d["range"]["start"]["character"]
                  for d in diags),
              "diags=%r" % (diags,))
        not_send = next(d for d in diags if d.get("code") == "E_NOT_SEND")
        check("diagnostic/range-points-at-error",
              not_send["range"]["start"]["line"] == 4,  # `total = ...` line
              "range=%r" % (not_send["range"],))

        # --- hover (heuristic) ----------------------------------------
        resp = client.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": 2, "character": 7},  # on `total`
        })
        value = ((resp.get("result") or {}).get("contents") or {}).get(
            "value", "")
        check("hover/returns-symbol-info",
              "total" in value and "Int" in value,
              "value=%r" % (value,))
        resp = client.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": 3, "character": 1},  # whitespace
        })
        check("hover/empty-spot-is-null", resp.get("result") is None,
              "result=%r" % (resp.get("result"),))

        # --- didChange fixes the error ---------------------------------
        client.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": GOOD_SOURCE}],
        })
        diags = client.wait_diagnostics(uri)
        check("didChange/clears-diagnostics", diags == [],
              "diags=%r" % (diags,))

        # --- unknown method -------------------------------------------
        resp = client.request("nope/notAMethod", {})
        check("unknown-method/error",
              resp.get("error", {}).get("code") == -32601,
              "resp=%r" % (resp,))

        # --- shutdown / exit -------------------------------------------
        resp = client.request("shutdown", None)
        check("shutdown/returns-null", resp.get("result") is None,
              "resp=%r" % (resp,))
        client.notify("exit", None)
        code = proc.wait(timeout=10)
        check("exit/clean", code == 0, "exit code=%r" % (code,))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        err = proc.stderr.read().decode("utf-8", "replace")
        if failures and err.strip():
            print("--- server stderr ---")
            print(err[-2000:])

    print()
    if failures:
        print("%d LSP FAILURE(S)" % len(failures))
        return 1
    print("lsp: all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
