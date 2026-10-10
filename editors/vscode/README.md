# Nova for VS Code

Syntax highlighting, snippets, and LSP-powered diagnostics + hover for `.nova` files.

## Features

- **TextMate grammar** (`syntaxes/nova.tmLanguage.json`) — keywords, types, components, view tags, strings, comments, and `null`/`nil`/`undefined` flagged as illegal (Nova has no null).
- **Snippets** — `component`, `aifn`, `serve`, `stream`, `matchr`, `matcho`, `data`, `effn`, `asyncfn`, `novatest`.
- **Language server** — `extension.js` launches `nova lsp` over stdio for real diagnostics and hover, using the same checker as `nova check`.

## Install (development)

```bash
cd editors/vscode
npm install          # installs vscode-languageclient
# then in VS Code: Run > Start Debugging, or copy this folder to
# ~/.vscode/extensions/nova-lang-0.1.0/ and reload.
```

Make sure the `nova` binary is on your PATH (repo root: `./nova`). If it lives
elsewhere, set it in settings:

```json
{ "nova.serverPath": "/path/to/nova-lang/nova" }
```

## Package a .vsix

```bash
npm install -g @vscode/vsce
vsce package        # produces nova-lang-0.1.0.vsix
```

Install the `.vsix` via `Extensions > ... > Install from VSIX`, or:

```bash
code --install-extension nova-lang-0.1.0.vsix
```

## Why VS Code (and not a custom IDE)

Developers already live here. Nova ships `nova lsp` with real diagnostics, so a
thin client extension gets us highlighting + errors + hover on day one, in the
editor people actually use. A bespoke IDE is out of scope — deliberately.

## Status

`nova lsp` is experimental; hover is heuristic and completion is not yet
implemented. Diagnostics (including `E_NO_NULL`, `E_ASYNC_BLOCKING`,
`E_NOT_SEND`) are real and come straight from the checker.
