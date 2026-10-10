# Nova website

Static site for the Nova programming language. No build step to *view* — but run
`build.sh` once after cloning to vendor the interpreter sources, extract the
playground examples, and render the docs pages from markdown.

## View locally

```bash
./build.sh
# then serve over HTTP (the playground needs fetch(), which file:// blocks):
python3 -m http.server 8000
# open http://localhost:8000
```

Opening `index.html` directly from `file://` works for every page except the
live playground, which degrades gracefully to static snippets.

## Publish

Any static host works: GitHub Pages, Netlify, Cloudflare Pages, S3. Publish the
`website/` directory as-is after running `./build.sh`.

```bash
# GitHub Pages example (from the repo root):
# push, then enable Pages on the /website folder in repo settings.
```

## Playground

`playground.html` attempts to load the real `nova_lang` interpreter via Pyodide
(CDN, pinned v0.26.4) so the three examples are editable and runnable. If the
CDN is unreachable, the page is opened from `file://`, or the interpreter fails
its smoke test, the page falls back to static snippets with expected output and
a roadmap note — it never ships a broken interactive state.

## Structure

- `index.html` — hero, thesis, pillars, AI section
- `tour.html` — 13-stop guided language tour
- `snippets.html` — snippet library (service, component, ai fn, streams, Result, migrations)
- `playground.html` + `js/playground.js` — in-browser interpreter attempt + fallback
- `docs.html` + `docs/*.html` — generated from `../docs/*.md`, `../SPEC.md`, `../README.md`
- `download.html` — install instructions
- `css/style.css`, `js/main.js` — theme, nav, copy buttons, Nova syntax highlighter
- `build_docs.py` — stdlib-only markdown → HTML converter
- `nova_py/` — vendored interpreter sources (generated, do not edit)
