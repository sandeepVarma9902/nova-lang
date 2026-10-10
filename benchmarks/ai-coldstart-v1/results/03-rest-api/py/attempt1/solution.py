import json
from http.server import BaseHTTPRequestHandler, HTTPServer

USERS = {
    1: {"id": 1, "name": "Ada", "email": "ada@example.com"},
    2: {"id": 2, "name": "Ben", "email": "ben@example.com"},
    3: {"id": 3, "name": "Cara", "email": "cara@example.com"},
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "users":
            try:
                uid = int(parts[1])
            except ValueError:
                return self._send(400, {"error": "bad id"})
            if uid <= 0:
                return self._send(400, {"error": "bad id"})
            if uid not in USERS:
                return self._send(404, {"error": "not found"})
            return self._send(200, USERS[uid])
        return self._send(404, {"error": "not found"})

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 8931), H).serve_forever()
