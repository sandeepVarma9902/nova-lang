# manifest.py — the `app.nova` application manifest.
#
# The manifest is the platform layer around the small language core:
# convention-over-configuration for app name, threading model, declared
# resources (streams, secrets, cron schedules, buckets), environments,
# docker settings and the deploy target. It is OPTIONAL — `nova run`
# works fine without one — but built in from the start.
#
# Example:
#   app "todo-service" {
#     version = "0.1.0"
#     entry = "main.nova"
#     threading = "auto"          // single | multi | auto
#
#     env dev { log_level = "debug" }
#     env prod { log_level = "info" }
#
#     resource stream "events" { partitions = 4 }
#     resource secret "db-password" { store = "env" }
#     resource cron "nightly-report" { schedule = "0 2 * * *", run = "report.nova" }
#     resource bucket "uploads" { }
#
#     docker { base = "python:3.12-slim", port = 8080 }
#     deploy target = "local"     // local | docker
#   }

import os


class ManifestError(Exception):
    def __init__(self, message, line=0):
        super().__init__(message)
        self.line = line


class _Tok:
    __slots__ = ("kind", "value", "line")

    def __init__(self, kind, value, line):
        self.kind = kind
        self.value = value
        self.line = line


def _tokenize(src, path="<manifest>"):
    toks = []
    i, line, n = 0, 1, len(src)
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif c == "#":
            while i < n and src[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and src[j] != '"':
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j + 1])
                    j += 2
                else:
                    buf.append(src[j])
                    j += 1
            if j >= n:
                raise ManifestError("unterminated string", line)
            toks.append(_Tok("STRING", "".join(buf), line))
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_-"):
                j += 1
            toks.append(_Tok("IDENT", src[i:j], line))
            i = j
        elif c.isdigit():
            j = i
            while j < n and src[j].isdigit():
                j += 1
            toks.append(_Tok("INT", int(src[i:j]), line))
            i = j
        elif c in "{}=":
            toks.append(_Tok(c, c, line))
            i += 1
        elif c == ",":
            i += 1  # separators are optional in kv blocks
        else:
            raise ManifestError("unexpected character %r" % c, line)
    toks.append(_Tok("EOF", "", line))
    return toks


class Manifest:
    def __init__(self, name):
        self.name = name
        self.version = "0.1.0"
        self.entry = "main.nova"
        self.threading = "auto"
        self.envs = {}          # env name -> {key: value}
        self.resources = []     # [(kind, name, {opts})]
        self.docker = {}        # {key: value}
        self.deploy_target = "local"

    def env(self, name):
        return self.envs.get(name, {})


class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.pos = 0

    def peek(self):
        return self.toks[self.pos]

    def next(self):
        t = self.peek()
        self.pos += 1
        return t

    def expect(self, kind, what):
        t = self.next()
        if t.kind != kind:
            raise ManifestError("expected %s, found %r" % (what, t.value),
                                t.line)
        return t

    def parse(self):
        t = self.expect("IDENT", "'app'")
        if t.value != "app":
            raise ManifestError("manifest must start with app \"name\"",
                                t.line)
        name = self.expect("STRING", "app name").value
        m = Manifest(name)
        self.expect("{", "'{'")
        while self.peek().kind != "}":
            self.parse_item(m)
        self.expect("}", "'}'")
        self._validate(m)
        return m

    def parse_item(self, m):
        t = self.expect("IDENT", "manifest key")
        k = t.value
        if k in ("version", "entry", "threading"):
            self.expect("=", "'='")
            v = self.expect("STRING", "string value").value
            setattr(m, k, v)
        elif k == "env":
            env_name = self.expect("IDENT", "environment name").value
            m.envs[env_name] = self.parse_kv_block()
        elif k == "resource":
            kind = self.expect("IDENT", "resource kind").value
            rname = self.expect("STRING", "resource name").value
            opts = self.parse_kv_block()
            m.resources.append((kind, rname, opts))
        elif k == "docker":
            m.docker = self.parse_kv_block()
        elif k == "deploy":
            t2 = self.expect("IDENT", "'target'")
            if t2.value != "target":
                raise ManifestError("expected 'target' after deploy",
                                    t2.line)
            self.expect("=", "'='")
            m.deploy_target = self.expect("STRING", "deploy target").value
        else:
            raise ManifestError("unknown manifest key '%s'" % k, t.line)

    def parse_kv_block(self):
        self.expect("{", "'{'")
        out = {}
        while self.peek().kind != "}":
            t = self.expect("IDENT", "key")
            self.expect("=", "'='")
            v = self.next()
            if v.kind not in ("STRING", "INT"):
                raise ManifestError("expected string or int value", v.line)
            out[t.value] = v.value
        self.expect("}", "'}'")
        return out

    def _validate(self, m):
        if m.threading not in ("single", "multi", "auto"):
            raise ManifestError(
                "threading must be single|multi|auto, got '%s'"
                % m.threading)
        if m.deploy_target not in ("local", "docker", "aws", "gcp"):
            raise ManifestError(
                "deploy target must be local|docker|aws|gcp, got '%s'"
                % m.deploy_target)
        for kind, name, _ in m.resources:
            if kind not in ("stream", "secret", "cron", "bucket"):
                raise ManifestError(
                    "unknown resource kind '%s' (want stream|secret|cron|"
                    "bucket)" % kind)


def load_manifest(path):
    """Parse and validate app.nova at path. Raises ManifestError."""
    with open(path) as f:
        src = f.read()
    toks = _tokenize(src, path)
    return _Parser(toks).parse()


def find_manifest(entry_path):
    """Convention: app.nova lives next to the entry file."""
    cand = os.path.join(os.path.dirname(os.path.abspath(entry_path)),
                        "app.nova")
    return cand if os.path.isfile(cand) else None


def resolve_threading(entry_path):
    """Threading model for a run: NOVA_THREADING env > app.nova > auto."""
    env = os.environ.get("NOVA_THREADING", "").strip()
    if env:
        if env not in ("single", "multi", "auto"):
            raise ManifestError(
                "NOVA_THREADING must be single|multi|auto, got '%s'" % env)
        return env
    man_path = find_manifest(entry_path)
    if man_path:
        try:
            return load_manifest(man_path).threading
        except ManifestError as e:
            raise ManifestError("app.nova: %s" % e)
    return "auto"
