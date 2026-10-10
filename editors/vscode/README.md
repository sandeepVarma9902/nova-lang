# Nova for VS Code

Write Nova with syntax highlighting, snippets, one-click Run, and LSP diagnostics — right in the editor you already use.

## Install (2 minutes)

1. Make sure the `nova` binary works: `nova --version` (repo root has `./nova`).
2. In this folder: `npm install`
3. Package it: `npx vsce package`
4. In VS Code: `Extensions → … → Install from VSIX` and pick the `.vsix` file.

That's it. The extension finds `nova` on your PATH automatically. If it's somewhere unusual, you'll get a friendly prompt to point at it once — or set it directly:

```json
{ "nova.serverPath": "/path/to/nova-lang/nova" }
```

## What you get

- **Syntax highlighting** for `.nova` files (keywords, types, components, strings — and `null`/`nil`/`undefined` flagged, because Nova has no null).
- **10 snippets**: `component`, `aifn`, `serve`, `stream`, `matchr`, `matcho`, `data`, `effn`, `asyncfn`, `novatest`.
- **LSP diagnostics + hover** from the real Nova checker (`nova lsp`).
- **Commands** (command palette or right-click): *Nova: Run current file*, *Check current file*, *Format current file*, *Restart language server*. Format-on-save works too.
- **Status bar**: `$(check) Nova` means the language server is healthy.

## Make it yours (settings)

| Setting | Default | What it does |
|---|---|---|
| `nova.serverPath` | `"nova"` | Path to the nova binary (auto-detected from PATH). |
| `nova.lsp.enabled` | `true` | Turn the language server off if you only want highlighting + snippets. |
| `nova.lsp.args` | `[]` | Extra args for `nova lsp`. |
| `nova.run.args` | `[]` | Extra args appended to `nova run <file>`. |
| `nova.trace.server` | `"off"` | LSP message tracing for debugging. |

All settings work per-workspace or globally — configure Nova differently for each project.

## Status

`nova lsp` diagnostics and hover are real (straight from the checker). Completion is not implemented yet.
