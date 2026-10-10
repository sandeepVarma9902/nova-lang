/* Nova Language VS Code extension — LSP client.
 *
 * Spawns `nova lsp` (stdio JSON-RPC) for diagnostics and hover on .nova files.
 * The server path is configurable via the `nova.serverPath` setting.
 *
 * Install deps once:  npm install
 * Package:            npx vsce package
 */
"use strict";

const vscode = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

let client = null;

function activate(context) {
  const config = vscode.workspace.getConfiguration("nova");
  const serverPath = config.get("serverPath", "nova");
  const trace = config.get("trace.server", "off");

  const serverOptions = {
    command: serverPath,
    args: ["lsp"],
    transport: TransportKind.stdio,
  };

  const clientOptions = {
    documentSelector: [{ scheme: "file", language: "nova" }],
    synchronize: {
      configurationSection: "nova",
    },
    traceOutputChannel: trace === "off" ? undefined : vscode.window.createOutputChannel("Nova LSP trace"),
  };

  client = new LanguageClient(
    "nova",
    "Nova Language Server",
    serverOptions,
    clientOptions
  );

  // Surface a friendly error if `nova` isn't on PATH.
  client.onDidChangeState(({ newState }) => {
    const { State } = require("vscode-languageclient/node");
    if (newState === State.Stopped) {
      vscode.window.showWarningMessage(
        "Nova language server stopped. Is `nova` on your PATH? " +
        "Set it via the `nova.serverPath` setting."
      );
    }
  });

  client.start();
  context.subscriptions.push(client);
}

function deactivate() {
  if (!client) return undefined;
  return client.stop();
}

module.exports = { activate, deactivate };
