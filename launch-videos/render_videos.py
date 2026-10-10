#!/usr/bin/env python3
"""Render 4 Nova launch demo videos (1080x1920, 30fps, silent).

EVERYTHING on screen is real: code is the actual Nova source that was
executed with the real ./nova CLI, and every terminal line below is the
genuine captured output. Nothing is invented.

Captured 2026-10-10 from ~/workspace/nova-lang (verified again after rebuild):
  $ ./nova check <file>                       -> []
  $ ./nova run web.nova      (port 8091)      -> [boot] listening on http://127.0.0.1:8091
  $ curl localhost:8091/hello                -> "Hello from Nova Boot"
  $ ./nova run api.nova      (port 8092)      -> [boot] listening on http://127.0.0.1:8092
  $ curl localhost:8092/users/7              -> "user 7"
  $ curl localhost:8092/users/abc            -> {"error": "path param 'id' must be Int"}
  $ nova dom counter.nova -o counter.html    -> dom: counter.nova -> counter.html
"""
import os
import re
import subprocess
import sys
import tempfile
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
BG = (13, 17, 23)
PANE = (22, 27, 34)
HEADER = (30, 36, 44)
TXT = (214, 222, 235)
DIM = (139, 148, 158)
VIOLET = (199, 146, 234)
GOLD = (255, 203, 107)
GREEN = (195, 232, 141)
GRAYC = (84, 110, 122)
ORANGE = (247, 140, 108)
BLUE = (130, 170, 255)
PROMPT = (126, 231, 135)
ACCENT = (163, 113, 247)

FD = "/usr/share/fonts/truetype/dejavu/"
CODE_FONT = ImageFont.truetype(FD + "DejaVuSansMono.ttf", 26)
SMALL = ImageFont.truetype(FD + "DejaVuSansMono.ttf", 24)
TITLE = ImageFont.truetype(FD + "DejaVuSans-Bold.ttf", 76)
SUB = ImageFont.truetype(FD + "DejaVuSans.ttf", 36)
BIG = ImageFont.truetype(FD + "DejaVuSans-Bold.ttf", 120)
URLF = ImageFont.truetype(FD + "DejaVuSans.ttf", 40)
DIAG = ImageFont.truetype(FD + "DejaVuSans-Bold.ttf", 40)
DIAG_S = ImageFont.truetype(FD + "DejaVuSans.ttf", 30)

KEYWORDS = {"import", "let", "var", "fn", "effect", "io", "return", "if", "else",
            "for", "in", "match", "data", "class", "component", "state", "view",
            "render", "use", "true", "false", "not", "and", "or", "None",
            "Some", "Ok", "Err", "boot"}
TYPES = {"Int", "String", "Float", "Bool", "List", "Option", "Result",
         "Request", "Response", "IO", "Post", "Counter"}
BUILTINS = {"print", "str", "router", "serve", "get", "post", "put", "delete",
            "jsonResponse", "paramInt", "jsonGet", "jsonStr", "query",
            "reqMethod", "env", "parseInt", "jsonErr", "needJson", "ok"}

TOKEN_RE = re.compile(
    r'(?P<comment>//.*$)|(?P<string>"(?:[^"\\]|\\.)*")'
    r'|(?P<number>\b\d+(?:\.\d+)?\b)|(?P<tag></?[A-Za-z][\w]*>?)'
    r'|(?P<word>[A-Za-z_][\w]*)|(?P<other>.)')

_line_cache = {}
frames_drawn = []  # blink clock


def highlight_line(line):
    """Return list of (text, color) tokens for a Nova source line."""
    if line in _line_cache:
        return _line_cache[line]
    toks = []
    prev_word = None
    for m in TOKEN_RE.finditer(line):
        kind = m.lastgroup
        s = m.group()
        if kind == "comment":
            toks.append((s, GRAYC))
        elif kind == "string":
            toks.append((s, GREEN))
        elif kind == "number":
            toks.append((s, ORANGE))
        elif kind == "tag":
            name = s.strip("</>")
            i = s.find(name)
            toks.append((s[:i], GRAYC))
            toks.append((name, GOLD))
            toks.append((s[i + len(name):], GRAYC))
        elif kind == "word":
            if prev_word == "fn":
                toks.append((s, BLUE))
            elif s in KEYWORDS:
                toks.append((s, VIOLET))
            elif s in TYPES:
                toks.append((s, GOLD))
            elif s in BUILTINS:
                toks.append((s, BLUE))
            elif s == "web":
                toks.append((s, DIM))
            else:
                toks.append((s, TXT))
            prev_word = s
            continue
        else:
            toks.append((s, TXT))
        if kind != "word":
            prev_word = None
    _line_cache[line] = toks
    return toks


def line_width(toks, font=CODE_FONT):
    return sum(font.getlength(t) for t, _ in toks)


def draw_tokens(draw, toks, x, y, font=CODE_FONT, upto=None):
    n = 0
    for t, color in toks:
        if upto is not None:
            if n >= upto:
                break
            t = t[:upto - n]
            n += len(t)
        draw.text((x, y), t, font=font, fill=color)
        x += font.getlength(t)
    return x


def blank():
    return Image.new("RGB", (W, H), BG)


def title_card_frames(title, subtitle):
    for _ in range(30):
        img = blank()
        d = ImageDraw.Draw(img)
        tw = d.textlength(title, font=TITLE)
        d.text(((W - tw) / 2, 820), title, font=TITLE, fill=TXT)
        sw = d.textlength(subtitle, font=SUB)
        d.text(((W - sw) / 2, 940), subtitle, font=SUB, fill=DIM)
        d.line([(W // 2 - 60, 1010), (W // 2 + 60, 1010)], fill=ACCENT, width=6)
        yield img


def end_card_frames():
    for _ in range(60):
        img = blank()
        d = ImageDraw.Draw(img)
        t = "Nova"
        tw = d.textlength(t, font=BIG)
        d.text(((W - tw) / 2, 800), t, font=BIG, fill=TXT)
        u = "github.com/sandeepVarma9902/nova-lang"
        uw = d.textlength(u, font=URLF)
        d.text(((W - uw) / 2, 970), u, font=URLF, fill=ACCENT)
        yield img


def pane_header(d, y, h, label):
    d.rounded_rectangle([40, y, W - 40, y + h], radius=14, fill=HEADER)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([62 + i * 34, y + h // 2 - 9, 62 + i * 34 + 18, y + h // 2 + 9], fill=c)
    d.text((150, y + h // 2 - 16), label, font=SMALL, fill=DIM)


def editor_state(d, filename, lines, chars_shown):
    pane_header(d, 90, 60, filename)
    d.rounded_rectangle([40, 160, W - 40, 1150], radius=14, fill=PANE)
    y = 200
    lh = 42
    for i, line in enumerate(lines):
        shown = chars_shown[i]
        if shown > 0:
            num = str(i + 1)
            d.text((66, y), num, font=SMALL, fill=GRAYC)
            draw_tokens(d, highlight_line(line), 130, y - 4, upto=shown)
        y += lh
    last = max([i for i, c in enumerate(chars_shown) if c > 0], default=-1)
    if last >= 0:
        drawn = []
        rem = chars_shown[last]
        for t, c in highlight_line(lines[last]):
            if rem <= 0:
                break
            drawn.append((t[:rem], c))
            rem -= len(t)
        cx = 130 + line_width(drawn)
        cy = 200 + last * lh - 4
        if (len(frames_drawn) // 15) % 2 == 0:
            d.rectangle([cx + 2, cy, cx + 18, cy + 36], fill=ACCENT)


def type_code_frames(lines, filename):
    total = [len(l) for l in lines]
    shown = [0] * len(lines)
    li = 0
    while li < len(lines):
        if lines[li].strip() == "":
            shown[li] = 1
            li += 1
            continue
        while shown[li] < total[li]:
            shown[li] = min(total[li], shown[li] + 4)
            img = blank()
            d = ImageDraw.Draw(img)
            editor_state(d, filename, lines, shown)
            frames_drawn.append(1)
            yield img
        li += 1
        for _ in range(2):
            img = blank()
            d = ImageDraw.Draw(img)
            editor_state(d, filename, lines, shown)
            frames_drawn.append(1)
            yield img
    for _ in range(30):
        img = blank()
        d = ImageDraw.Draw(img)
        editor_state(d, filename, lines, shown)
        frames_drawn.append(1)
        yield img


def draw_terminal(d, term_lines):
    d.rounded_rectangle([40, 1200, W - 40, 1800], radius=14, fill=PANE)
    pane_header(d, 1200, 56, "terminal")
    y = 1290
    for text, color in term_lines:
        while CODE_FONT.getlength(text) > W - 160:
            cut = max(1, int((W - 160) / CODE_FONT.getlength("x")))
            d.text((80, y), text[:cut], font=CODE_FONT, fill=color)
            text = text[cut:]
            y += 42
        d.text((80, y), text, font=CODE_FONT, fill=color)
        y += 42


def terminal_frames(steps, lines, filename):
    shown = [len(l) for l in lines]
    term_lines = []
    for cmd, outs in steps:
        typed = ""
        full = "$ " + cmd
        while len(typed) < len(full):
            typed = full[:len(typed) + 4]
            img = blank()
            d = ImageDraw.Draw(img)
            editor_state(d, filename, lines, shown)
            draw_terminal(d, term_lines + [(typed, PROMPT)])
            frames_drawn.append(1)
            yield img
        term_lines.append((full, PROMPT))
        for _ in range(15):
            img = blank()
            d = ImageDraw.Draw(img)
            editor_state(d, filename, lines, shown)
            draw_terminal(d, term_lines)
            frames_drawn.append(1)
            yield img
        for o in outs:
            term_lines.append((o, TXT))
            for _ in range(8):
                img = blank()
                d = ImageDraw.Draw(img)
                editor_state(d, filename, lines, shown)
                draw_terminal(d, term_lines)
                frames_drawn.append(1)
                yield img
        for _ in range(30):
            img = blank()
            d = ImageDraw.Draw(img)
            editor_state(d, filename, lines, shown)
            draw_terminal(d, term_lines)
            frames_drawn.append(1)
            yield img


def browser_preview_frames():
    # Faithful render of the REAL generated counter.html DOM:
    # h1 "Clicks", p "0", buttons "Increment" / "Decrement"
    # (strings extracted from the actual `nova dom` output).
    b64 = ImageFont.truetype(FD + "DejaVuSans-Bold.ttf", 64)
    t56 = ImageFont.truetype(FD + "DejaVuSans.ttf", 56)
    for _ in range(150):
        img = blank()
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([40, 400, W - 40, 1500], radius=24, fill=(255, 255, 255))
        d.rounded_rectangle([40, 400, W - 40, 500], radius=24, fill=(230, 232, 236))
        d.rectangle([40, 470, W - 40, 500], fill=(230, 232, 236))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([70 + i * 36, 436, 70 + i * 36 + 20, 456], fill=c)
        d.rounded_rectangle([180, 428, W - 70, 472], radius=12, fill=(255, 255, 255))
        d.text((200, 434), "counter.html", font=SMALL, fill=(90, 90, 90))
        d.text((90, 580), "Clicks", font=b64, fill=(20, 20, 20))
        d.text((90, 700), "0", font=t56, fill=(20, 20, 20))
        d.rounded_rectangle([90, 820, 330, 900], radius=14, fill=(37, 99, 235))
        bw = d.textlength("Increment", font=SUB)
        d.text((210 - bw / 2, 842), "Increment", font=SUB, fill=(255, 255, 255))
        d.rounded_rectangle([360, 820, 600, 900], radius=14, fill=(220, 224, 230))
        bw = d.textlength("Decrement", font=SUB)
        d.text((480 - bw / 2, 842), "Decrement", font=SUB, fill=(40, 40, 40))
        yield img


def diagram_frames():
    boxes = [
        ("Client", "curl / browser"),
        ("Nova Boot API", "typed routes"),
        ("Streams", "events"),
        ("SQLite", "storage"),
    ]
    appear = [15, 80, 145, 210]
    total = 300
    bw, bh = 640, 120
    ys = [560, 830, 1100, 1370]
    for f in range(total):
        img = blank()
        d = ImageDraw.Draw(img)
        t = "From HLD to LLD"
        tw = d.textlength(t, font=TITLE)
        d.text(((W - tw) / 2, 220), t, font=TITLE, fill=TXT)
        s = "the architecture, then the code"
        sw = d.textlength(s, font=SUB)
        d.text(((W - sw) / 2, 340), s, font=SUB, fill=DIM)
        for i, (label, sub) in enumerate(boxes):
            if f < appear[i]:
                continue
            a = min(1.0, (f - appear[i]) / 18)
            sc = 0.7 + 0.3 * a
            w2, h2 = bw * sc, bh * sc
            x1 = W / 2 - w2 / 2
            y1 = ys[i] + (bh - h2) / 2
            d.rounded_rectangle([x1, y1, x1 + w2, y1 + h2], radius=18,
                               outline=ACCENT, width=3)
            lw = d.textlength(label, font=DIAG)
            d.text(((W - lw) / 2, y1 + 16), label, font=DIAG, fill=TXT)
            sw2 = d.textlength(sub, font=DIAG_S)
            d.text(((W - sw2) / 2, y1 + 66), sub, font=DIAG_S, fill=DIM)
            if i > 0:
                prog = min(1.0, (f - appear[i]) / 25)
                x = W / 2
                y_start = ys[i - 1] + bh
                y_end = ys[i]
                yy = y_start + (y_end - y_start) * prog
                d.line([(x, y_start), (x, yy)], fill=ACCENT, width=5)
                if prog >= 1.0:
                    d.polygon([(x - 12, yy - 18), (x + 12, yy - 18), (x, yy)], fill=ACCENT)
        if f > 262:
            msg = "zooming into the LLD"
            mw = d.textlength(msg, font=SUB)
            d.text(((W - mw) / 2, 1560), msg, font=SUB, fill=ACCENT)
        yield img


def encode(frames, out_path):
    errfile = tempfile.NamedTemporaryFile(delete=False, suffix=".log").name
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
           "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "pipe:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", "21", "-preset", "veryfast", "-movflags", "+faststart",
           out_path]
    with open(errfile, "w") as ef:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=ef)
        n = 0
        try:
            for img in frames:
                p.stdin.write(img.tobytes())
                n += 1
        finally:
            try:
                p.stdin.close()
            except BrokenPipeError:
                pass
            p.wait()
    if p.returncode != 0:
        with open(errfile) as ef:
            tail = ef.read()[-3000:]
        raise RuntimeError(f"ffmpeg failed rc={p.returncode} for {out_path}:\n{tail}")
    os.unlink(errfile)
    return n


def chain(*gens):
    for g in gens:
        yield from g


WEB_CODE = '''import std.web

effect fn hello(req: Request) -> Result[Response, Response] {
  return Ok(jsonResponse(200, "Hello from Nova Boot"))
}

effect io fn boot(cap: IO) {
  let r = router()
  get(r, "/hello", hello, cap)
  serve(r, 8091, cap)
}

boot(io)'''.split("\n")

API_CODE = '''import std.web

effect fn user(req: Request) -> Result[Response, Response] {
  let id = match paramInt(req, "id") {
    Some(i) => Ok(i),
    None => Err(web.jsonErr(400, "id must be an Int"))
  }?
  return Ok(jsonResponse(200, "user " + str(id)))
}

effect io fn boot(cap: IO) {
  let r = router()
  get(r, "/users/{id: Int}", user, cap)
  serve(r, 8092, cap)
}

boot(io)'''.split("\n")

UI_CODE = '''component Counter(title: String) {
  state count: Int = 0

  view {
    <div>
      <h1>{title}</h1>
      <p>{count}</p>
      <button onClick={count = count + 1}>Increment</button>
      <button onClick={count = count - 1}>Decrement</button>
    </div>
  }
}

render(Counter(title: "Clicks"))'''.split("\n")

LLD_CODE = '''effect fn user(req: Request) -> Result[Response, Response] {
  let id = match paramInt(req, "id") {
    Some(i) => Ok(i),
    None => Err(web.jsonErr(400, "id must be an Int"))
  }?
  return Ok(jsonResponse(200, "user " + str(id)))
}'''.split("\n")


def video_web_server(out):
    steps = [
        ("./nova run web.nova", ["[boot] listening on http://127.0.0.1:8091"]),
        ("curl localhost:8091/hello", ['"Hello from Nova Boot"']),
    ]
    return encode(chain(
        title_card_frames("A web server in 8 lines", "Nova Boot \u00b7 zero config"),
        type_code_frames(WEB_CODE, "web.nova"),
        terminal_frames(steps, WEB_CODE, "web.nova"),
        end_card_frames(),
    ), out)


def video_api(out):
    steps = [
        ("./nova run api.nova", ["[boot] listening on http://127.0.0.1:8092"]),
        ("curl localhost:8092/users/7", ['"user 7"']),
        ("curl localhost:8092/users/abc", ['{"error": "path param \'id\' must be Int"}']),
    ]
    return encode(chain(
        title_card_frames("Typed API routes", "path params are checked at the edge"),
        type_code_frames(API_CODE, "api.nova"),
        terminal_frames(steps, API_CODE, "api.nova"),
        end_card_frames(),
    ), out)


def video_ui(out):
    steps = [
        ("nova dom counter.nova -o counter.html", ["dom: counter.nova -> counter.html"]),
    ]
    return encode(chain(
        title_card_frames("UI code + live preview", "components compile to real DOM"),
        type_code_frames(UI_CODE, "counter.nova"),
        terminal_frames(steps, UI_CODE, "counter.nova"),
        browser_preview_frames(),
        end_card_frames(),
    ), out)


def video_design(out):
    steps = [
        ("nova check api.nova", ["[]"]),
        ("curl localhost:8092/users/abc", ['{"error": "path param \'id\' must be Int"}']),
    ]
    return encode(chain(
        diagram_frames(),
        title_card_frames("LLD: the route handler", "typed, checked, running"),
        type_code_frames(LLD_CODE, "api.nova"),
        terminal_frames(steps, LLD_CODE, "api.nova"),
        end_card_frames(),
    ), out)


if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(outdir, exist_ok=True)
    only = sys.argv[2] if len(sys.argv) > 2 else None
    jobs = [
        ("nova-web-server.mp4", video_web_server),
        ("nova-api.mp4", video_api),
        ("nova-ui.mp4", video_ui),
        ("nova-design.mp4", video_design),
    ]
    for name, fn in jobs:
        if only and only not in name:
            continue
        path = os.path.join(outdir, name)
        n = fn(path)
        print(f"{name}: {n} frames = {n / FPS:.1f}s", flush=True)
