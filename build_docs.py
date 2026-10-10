#!/usr/bin/env python3
"""Convert Nova markdown docs to styled HTML pages. Stdlib only.

Usage: python3 build_docs.py
Reads ../docs/*.md, ../SPEC.md, ../README.md and writes docs/*.html
with the site chrome (nav + stylesheet).
"""
import html
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)

# (source markdown, output html, page title)
PAGES = [
    ("README.md", "docs/overview.html", "Overview"),
    ("docs/tutorial.md", "docs/tutorial.html", "Tutorial — Learn Nova in 30 minutes"),
    ("docs/faq.md", "docs/faq.html", "FAQ"),
    ("docs/migration-guide.md", "docs/migration-guide.html", "Migration Guide"),
    ("docs/deployment.md", "docs/deployment.html", "Deployment"),
    ("SPEC.md", "docs/spec.html", "Language Specification"),
    ("docs/v2-bytecode.md", "docs/v2-bytecode.html", "NovaVM Bytecode"),
    ("docs/v2-deploy-cloud.md", "docs/v2-deploy-cloud.html", "Cloud Deployment"),
    ("docs/v2-eval.md", "docs/v2-eval.html", "nova eval Harness"),
    ("docs/v2-kafka.md", "docs/v2-kafka.html", "Kafka Streams Provider"),
    ("docs/v2-lsp.md", "docs/v2-lsp.html", "Language Server"),
    ("docs/v2-migrate.md", "docs/v2-migrate.html", "nova migrate"),
    ("docs/v2-render-dom.md", "docs/v2-render-dom.html", "Browser Renderer"),
]

CHROME = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Nova</title>
<link rel="stylesheet" href="../css/style.css">
</head>
<body>
<header class="site-header">
  <nav class="nav">
    <a class="brand" href="../index.html"><span class="brand-mark">&#10022;</span> Nova</a>
    <button class="nav-toggle" aria-label="Menu">&#9776;</button>
    <div class="nav-links">
      <a href="../index.html">Home</a>
      <a href="../tour.html">Tour</a>
      <a href="../snippets.html">Snippets</a>
      <a href="../playground.html">Playground</a>
      <a href="../docs.html" class="active">Docs</a>
      <a href="../download.html" class="cta">Download</a>
    </div>
  </nav>
</header>
<main class="doc-page">
  <aside class="doc-toc">
    <h4>Documentation</h4>
    {toc}
  </aside>
  <article class="doc-body">
{body}
  </article>
</main>
<footer class="site-footer">
  <p>Nova — Java's discipline &times; React's soul, written for AI.</p>
  <p class="credit">Designed by Sandeep Varma &amp; Muse.</p>
</footer>
<script src="../js/main.js?v=6"></script>
<script src="../js/nova-live.js?v=6"></script>
<script src="../js/runnable.js?v=6"></script>
</body>
</html>
"""


def esc(s):
    return html.escape(s)


def inline(s):
    s = esc(s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
              lambda m: '<a href="%s">%s</a>' % (_md_link(m.group(2)), m.group(1)), s)
    return s


def _md_link(url):
    # relative links to sibling markdown docs -> generated .html pages
    if re.match(r"^[a-zA-Z0-9_./-]+\.md(#.*)?$", url):
        return re.sub(r"\.md(#|$)", r".html\1", url)
    return url


def md_to_html(text):
    lines = text.split("\n")
    out = []
    i = 0
    in_code = False
    code_lang = ""
    code_buf = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < len(lines):
        line = lines[i]
        # fenced code
        m = re.match(r"^```([\w-]*)\s*$", line)
        if m:
            if in_code:
                lang = code_lang
                extra = ""
                if lang == "nova-norun":
                    # illustrative / intentionally-broken / non-Nova blocks:
                    # highlight as Nova but never get a Run widget
                    lang = "nova"
                    extra = ' data-norun="1"'
                lang = ' class="language-%s"%s' % (lang, extra) if lang else ""
                out.append("<pre><code%s>%s</code></pre>"
                           % (lang, esc("\n".join(code_buf))))
                code_buf = []
                in_code = False
            else:
                close_list()
                in_code = True
                code_lang = m.group(1)
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_list()
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, inline(m.group(2)), lvl))
            i += 1
            continue
        # horizontal rule
        if re.match(r"^---+\s*$", line):
            close_list()
            out.append("<hr>")
            i += 1
            continue
        # table
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(
                r"^\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            close_list()
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in
                             lines[i].strip().strip("|").split("|")])
                i += 1
            t = ["<table><thead><tr>"]
            t += ["<th>%s</th>" % inline(h) for h in headers]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join("<td>%s</td>" % inline(c)
                                          for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue
        # list item
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % inline(m.group(1)))
            i += 1
            continue
        # blank
        if not line.strip():
            close_list()
            i += 1
            continue
        # paragraph (merge following non-blank, non-special lines)
        close_list()
        para = [line]
        i += 1
        while (i < len(lines) and lines[i].strip()
               and not re.match(r"^(#{1,6}\s|```|\s*[-*]\s|\|)", lines[i])):
            para.append(lines[i])
            i += 1
        out.append("<p>%s</p>" % inline(" ".join(para)))
    close_list()
    if in_code:  # unterminated fence
        out.append("<pre><code>%s</code></pre>" % esc("\n".join(code_buf)))
    return "\n".join(out)


def main():
    toc_items = "\n".join(
        '    <a href="%s">%s</a>' % (os.path.basename(dst), title)
        for _, dst, title in PAGES)
    for src, dst, title in PAGES:
        src_path = os.path.join(SRC, src)
        dst_path = os.path.join(HERE, dst)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        with open(src_path) as f:
            body = md_to_html(f.read())
        page = CHROME.format(title=esc(title), toc=toc_items, body=body)
        with open(dst_path, "w") as f:
            f.write(page)
        print("wrote", dst)


if __name__ == "__main__":
    main()
