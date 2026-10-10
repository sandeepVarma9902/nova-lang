#!/usr/bin/env python3
# test_boot.py — Nova Boot: real HTTP assertions against a live Nova server.
#
# Boots a small Nova app on an ephemeral port, then exercises routes,
# typed path params (200/400), 404s, middleware short-circuiting, and
# static files — all through stdlib urllib.

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOVA = os.path.join(ROOT, "nova")

APP = r'''
import std.web

effect fn hello(req: Request) -> Result[Response, Response] {
  let who = match query(req, "who") {
    Some(w) => Ok(w),
    None => Err(web.jsonErr(400, "missing ?who="))
  }?
  return Ok(web.ok("hello " + who))
}

effect fn showUser(req: Request) -> Result[Response, Response] {
  let id = match paramInt(req, "id") {
    Some(i) => Ok(i),
    None => Err(web.jsonErr(400, "missing id"))
  }?
  return Ok(jsonResponse(200, id * 2))
}

effect fn mw(req: Request) -> Option[Response] {
  return match header(req, "x-block") {
    Some(_) => Some(web.jsonErr(403, "blocked")),
    None => None
  }
}

effect io fn boot(cap: IO) {
  let r = router()
  use(r, mw)
  staticDir(r, "/static", "{STATICDIR}")
  get(r, "/hello", hello, cap)
  get(r, "/users/{id: Int}", showUser, cap)
  match serve(r, {PORT}, cap) {
    Ok(_) => print("stopped"),
    Err(e) => print("serve failed: " + e)
  }
}

boot(io)
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_ready(port, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/hello?who=x" % port,
                                   timeout=2)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def get(port, path, headers=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(port, path, data):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=data.encode(),
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    tmp = tempfile.mkdtemp(prefix="nova-boot-test-")
    static = os.path.join(tmp, "static")
    os.makedirs(static)
    with open(os.path.join(static, "app.css"), "w") as fh:
        fh.write("body { color: red; }\n")
    port = free_port()
    app_src = APP.replace("{PORT}", str(port)).replace("{STATICDIR}", static)
    app_path = os.path.join(tmp, "server.nova")
    with open(app_path, "w") as fh:
        fh.write(app_src)

    # the app must type-check first
    p = subprocess.run([NOVA, "check", app_path], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, "check failed: " + p.stdout + p.stderr

    srv = subprocess.Popen([NOVA, "run", app_path],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    try:
        assert wait_ready(port), "server did not become ready"
        failures = []

        def check(name, cond, detail=""):
            if not cond:
                failures.append("%s %s" % (name, detail))

        s, b = get(port, "/hello?who=Ada")
        check("hello 200", s == 200 and b == "hello Ada", "%r %r" % (s, b))

        s, b = get(port, "/hello")
        check("hello 400", s == 400 and json.loads(b)["error"]
              == "missing ?who=", "%r %r" % (s, b))

        s, b = get(port, "/users/21")
        check("typed param", s == 200 and b == "42", "%r %r" % (s, b))

        s, b = get(port, "/users/abc")
        check("bad param 400", s == 400 and "must be Int" in b,
              "%r %r" % (s, b))

        s, b = get(port, "/nope")
        check("404", s == 404 and "no route" in b, "%r %r" % (s, b))

        s, b = post(port, "/hello?who=Ada", "{}")
        check("404 post", s == 404, "%r %r" % (s, b))

        s, b = get(port, "/hello?who=Ada", {"X-Block": "1"})
        check("middleware 403", s == 403
              and json.loads(b)["error"] == "blocked", "%r %r" % (s, b))

        s, b = get(port, "/static/app.css")
        check("static", s == 200 and "color: red" in b, "%r %r" % (s, b))

        s, b = get(port, "/static/../app.nova")
        check("traversal blocked", s in (403, 404), "%r %r" % (s, b))

        if failures:
            print("FAILURES:")
            for f in failures:
                print("  -", f)
            return 1
        print("boot http: 9 assertions green")
        return 0
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    sys.exit(main())
