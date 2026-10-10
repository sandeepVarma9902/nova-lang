# Nova LSP — `nova lsp`

> **EXPERIMENTAL.** This is a skeleton server (v2 roadmap item). It speaks
> real LSP over stdio and its diagnostics are real, but hover is heuristic
> and the feature set is intentionally small. Do not depend on it yet.

## What it is

`nova_lang/lsp.py` is a Language Server Protocol server for Nova, written in
stdlib-only Python. It reads JSON-RPC messages from stdin (with
`Content-Length` framing) and writes responses/notifications to stdout.

## Running it

```bash
cd ~/workspace/nova-lang
python3 -m nova_lang.lsp
```

Once the CLI wiring lands, the intended entry point is:

```bash
nova lsp
```

The server logs to **stderr** only — stdout is the protocol wire. Any
stdout pollution breaks the framing.

## Pointing an editor at it

Any editor with a generic stdio LSP client can drive it. The recipe is the
same everywhere: **command** = the interpreter + module, **transport** =
stdio, **file type** = `nova` (files ending in `.nova`).

**Neovim** (nvim-lspconfig style):

```lua
local lspconfig = require('lspconfig')
local configs = require('lspconfig.configs')
configs.nova = {
  default_config = {
    cmd = { 'python3', '-m', 'nova_lang.lsp' },
    filetypes = { 'nova' },
    root_dir = lspconfig.util.root_pattern('.nova-manifest', '.git'),
  },
}
```

**Emacs** (eglot):

```elisp
(add-to-list 'eglot-server-programs
             '(nova-mode . ("python3" "-m" "nova_lang.lsp")))
```

**VS Code**: use a generic LSP client extension (e.g. "Generic LSP Client"
or the `vscode-languageclient` sample host) pointed at
`python3 -m nova_lang.lsp` with `nova` as the language id.

**Helix** (`languages.toml`):

```toml
[[language]]
name = "nova"
scope = "source.nova"
file-types = ["nova"]
language-servers = ["nova-lsp"]

[language-server.nova-lsp]
command = "python3"
args = ["-m", "nova_lang.lsp"]
```

**Sublime Text** (LSP package): add a client config with
`"command": ["python3", "-m", "nova_lang.lsp"]` and selector `source.nova`.

## What's implemented

| Method | Kind | Behavior |
|---|---|---|
| `initialize` | request | Returns capabilities: full text-document sync (`openClose` + `change: full`), `hoverProvider`. Plus `serverInfo` (`nova-lsp`, version). |
| `initialized` | notification | No-op. |
| `textDocument/didOpen` | notification | Stores the document, runs the checker, pushes diagnostics. |
| `textDocument/didChange` | notification | Full-sync replace; re-checks and pushes fresh diagnostics. |
| `textDocument/didClose` | notification | Drops the document from memory. |
| `textDocument/hover` | request | Heuristic symbol lookup (see below). |
| `shutdown` | request | Returns `null`; next `exit` leaves with code 0. |
| `exit` | notification | Exits 0 after `shutdown`, 1 otherwise. |

Unknown methods get a standard `-32601 Method not found` error.

## What's REAL vs heuristic

**Real — diagnostics.** `textDocument/publishDiagnostics` carries Nova's
own checker output: `check_source()` runs on every open/change, and each
`Diagnostic` is mapped to an LSP Diagnostic with:

- `range` — 1-based `line`/`col` converted to 0-based, end extended over
  the identifier at that spot
- `severity` — `1` (Error); the checker currently emits errors only
- `code` — the stable Nova code, e.g. `E_NOT_SEND`, `E_TYPE_MISMATCH`,
  `E_SYNTAX` — the same codes `nova check` prints as JSON
- `source` — `"nova"`, plus the checker's `hint` appended to the message

**Heuristic — hover.** Hover does *not* use the checker's scopes. It builds
a per-request declaration index by regex-scanning the open document
(`fn` with effect keyword + return type, `let`/`var` with annotation or a
literal-type guess, `state`, `class`, `interface`, `component`, and function
parameters) and returns the declaration under the cursor as Markdown. Every
hover response says so explicitly:

````markdown
```nova
fn greet(name: String) -> String
```
function · declared on line 3 · effect: `pure`
_Heuristic hover — declaration scan, not type-checked._
````

It is good for "what's this variable / what's this function's signature".
It is not semantic: it won't resolve imports, shadowing, method calls, or
anything the regexes don't see.

## Known limitations (experimental)

- Full-document sync only; incremental `contentChanges` are not supported.
- No pull diagnostics (`textDocument/diagnostic`), no completion, no
  go-to-definition, no formatting — the roadmap, not this skeleton.
- One document = one `check_source(..., is_entry=True)` run; module
  imports resolve against the file's real directory, unsaved-buffer-only
  files check as `untitled.nova` against the working directory.
- A checker crash is caught and surfaced as an `E_LSP_INTERNAL` diagnostic
  rather than killing the server.

## Tests

```bash
python3 tests/test_lsp.py
```

Spawns the server as a subprocess and speaks real LSP: initialize
(capability assertions), didOpen a file containing an `E_NOT_SEND` error
(asserts the code, severity, and range come back), hover over a variable
(asserts the heuristic returns its inferred type), didChange to a clean
file (asserts diagnostics clear), an unknown method (asserts `-32601`),
then `shutdown`/`exit` (asserts exit code 0). The existing suite
(`python3 tests/run_tests.py`) is unaffected and stays green.
