# web.py — Nova Boot: the batteries-included web framework runtime.
#
# Zero-config HTTP serving on the stdlib: routing with typed path params,
# middleware short-circuiting, static files, structured JSON errors, and
# request logging. The server is single-threaded (stdlib http.server) —
# requests are handled sequentially; a threaded/async server is future
# work (documented in docs/v2.1-boot.md).
#
# Design: Nova code builds a Router value with the `router`/`route`/`use`
# builtins; `serve(router, port, io)` blocks forever serving it. Handler
# functions are Nova `effect fn (req: Request) -> Response` values, called
# back from Python. A handler returning Result[Response, E] maps Err to a
# 500 JSON body — errors become HTTP responses, not crashes.

import json as _json
import mimetypes
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1


class RoutePatternError(Exception):
    pass


def parse_route_pattern(pat):
    """Parse '/users/{id: Int}/posts' into segments.

    Returns [("lit", "users"), ("param", "id", "Int"), ("lit", "posts")].
    Param types: Int, Float, String, Bool.
    """
    if not pat.startswith("/"):
        raise RoutePatternError("route must start with '/'")
    segs = []
    for raw in pat.strip("/").split("/"):
        if raw.startswith("{") and raw.endswith("}"):
            inner = raw[1:-1]
            if ":" not in inner:
                raise RoutePatternError(
                    "param '%s' needs a type, e.g. {id: Int}" % raw)
            pname, ptype = (p.strip() for p in inner.split(":", 1))
            if not pname.isidentifier():
                raise RoutePatternError("bad param name '%s'" % pname)
            if ptype not in ("Int", "Float", "String", "Bool"):
                raise RoutePatternError(
                    "param type must be Int|Float|String|Bool, got '%s'"
                    % ptype)
            segs.append(("param", pname, ptype))
        elif "{" in raw or "}" in raw:
            raise RoutePatternError("malformed segment '%s'" % raw)
        else:
            segs.append(("lit", raw))
    return segs


def _convert(ptype, text):
    try:
        if ptype == "Int":
            v = int(text, 10)
            if v < INT_MIN or v > INT_MAX:
                return None
            return v
        if ptype == "Float":
            return float(text)
        if ptype == "Bool":
            if text == "true":
                return True
            if text == "false":
                return False
            return None
        return text  # String
    except (ValueError, OverflowError):
        return None


class Route:
    def __init__(self, method, pattern, handler):
        self.method = method.upper()
        self.pattern = pattern
        self.segments = parse_route_pattern(pattern)
        self.handler = handler  # FuncValue

    def match(self, method, path):
        if self.method != method.upper():
            return None
        parts = path.strip("/").split("/")
        # "/" -> [""] after strip -> handle root
        if path.strip("/") == "":
            parts = []
        if len(parts) != len(self.segments):
            return None
        params = {}
        for seg, part in zip(self.segments, parts):
            if seg[0] == "lit":
                if seg[1] != part:
                    return None
            else:
                _, pname, ptype = seg
                v = _convert(ptype, urllib.parse.unquote(part))
                if v is None:
                    return ("badparam", pname, ptype)
                params[pname] = v
        return ("ok", params)


class VRouter:
    def __init__(self):
        self.routes = []       # [Route]
        self.middlewares = []  # [FuncValue] (req) -> Option[Response]
        self.statics = []      # [(prefix, dir)]


class VRequest:
    def __init__(self, method, path, query, headers, body):
        self.method = method
        self.path = path
        self.query = query            # {name: str}
        self.headers = headers        # {lower-name: str}
        self.body = body              # str
        self.path_params = {}         # {name: str} (raw strings)


class VResponse:
    def __init__(self, status, body, content_type="text/plain", headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}


def _json_error(status, message):
    return VResponse(status, _json.dumps({"error": message}),
                     "application/json")


class _Handler(BaseHTTPRequestHandler):
    server_version = "NovaBoot/0.1"

    def log_message(self, fmt, *args):  # quiet: we log our own lines
        pass

    def _dispatch(self):
        srv = self.server
        t0 = time.monotonic()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        query = {k: v[0] for k, v in
                 urllib.parse.parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = (self.rfile.read(length).decode("utf-8", "replace")
                if length else "")
        headers = {k.lower(): v for k, v in self.headers.items()}
        resp = srv.nova_dispatch(self.command, path, query, headers, body)
        data = resp.body.encode("utf-8")
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in resp.headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        ms = (time.monotonic() - t0) * 1000
        print("[boot] %s %s -> %d (%.1fms)"
              % (self.command, path, resp.status, ms), flush=True)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch
    do_PATCH = _dispatch
    do_HEAD = _dispatch
    do_OPTIONS = _dispatch


class BootServer(HTTPServer):
    allow_reuse_address = True
    daemon_threads = False

    def __init__(self, addr, router, dispatch):
        self.nova_router = router
        self.nova_dispatch = dispatch
        super().__init__(addr, _Handler)


def serve_router(interp, router, port, node):
    """Block forever serving router on port. Returns VResult."""
    from .interpreter import VResult, VOption
    from .types import UNIT, STRING

    def dispatch(method, path, query, headers, body):
        req = VRequest(method, path, query, headers, body)
        # Static files first (cheap, no Nova call).
        for prefix, directory in router.statics:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                rel = path[len(prefix):].lstrip("/")
                abs_base = os.path.abspath(directory)
                fpath = os.path.normpath(os.path.join(abs_base, rel))
                if not fpath.startswith(abs_base + os.sep) \
                        and fpath != abs_base:
                    return _json_error(403, "forbidden")
                if os.path.isdir(fpath):
                    fpath = os.path.join(fpath, "index.html")
                if os.path.isfile(fpath):
                    ctype, _ = mimetypes.guess_type(fpath)
                    with open(fpath, "rb") as fh:
                        data = fh.read()
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        text = data.decode("latin-1")
                    return VResponse(200, text, ctype or
                                     "application/octet-stream")
        # Middleware chain: first Some(response) short-circuits.
        for mw in router.middlewares:
            out = _call_nova(interp, mw, req, node)
            if isinstance(out, VOption) and out.value is not None:
                return out.value
        # Routes.
        badparam = None
        for route in router.routes:
            m = route.match(method, path)
            if m is None:
                continue
            if m[0] == "badparam":
                badparam = m
                continue
            _, params = m
            req.path_params = {k: str(v) for k, v in params.items()}
            req.typed_params = params
            out = _call_nova(interp, route.handler, req, node)
            if isinstance(out, VResult):
                if out.is_ok:
                    return out.value
                # Result-to-HTTP mapping: Err(Response) serves that
                # response (e.g. a 400); any other Err is a 500.
                if isinstance(out.value, VResponse):
                    return out.value
                return _json_error(
                    500, "handler error: %s" % out.value)
            return out
        if badparam is not None:
            _, pname, ptype = badparam
            return _json_error(
                400, "path param '%s' must be %s" % (pname, ptype))
        return _json_error(404, "no route for %s %s" % (method, path))

    try:
        server = BootServer(("127.0.0.1", port), router, dispatch)
    except OSError as e:
        return VResult(UNIT, STRING, False, "bind :%d: %s" % (port, e))
    print("[boot] listening on http://127.0.0.1:%d" % port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return VResult(UNIT, STRING, True, None)


def _call_nova(interp, fv, req, node):
    """Call a Nova handler fn with a VRequest; map NovaError to 500."""
    from .interpreter import NovaError, TOP_EFF
    saved = interp.fx
    interp.fx = TOP_EFF
    try:
        return interp.call_fn(fv, [(None, req)], node, "handler")
    except NovaError as e:
        return _json_error(500, "%s: %s" % (e.code, e))
    except Exception as e:  # never let Python exceptions escape to HTTP
        return _json_error(500, "internal: %s" % e)
    finally:
        interp.fx = saved
