/* Nova Language VS Code extension.
 *
 * - Syntax highlighting, snippets, bracket matching (via package.json).
 * - LSP client: spawns `<nova> lsp` (stdio JSON-RPC) for diagnostics + hover.
 * - Commands: Run / Check / Format the current .nova file, restart the server.
 * - The nova binary is auto-detected (setting, PATH, common locations);
 *   if missing, the user is guided to pick it once.
 *
 * Install deps once:  npm install
 * Package:            npx vsce package
 */
"use strict";

const vscode = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");
const { execFile } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

let client = null;
let statusBar = null;
let novaBin = null;
const out = vscode.window.createOutputChannel("Nova");

function cfg() {
  return vscode.workspace.getConfiguration("nova");
}

/** Resolve the nova binary: setting > PATH > common locations. */
async function resolveNovaBin() {
  const configured = cfg().get("serverPath", "nova");
  if (configured && configured !== "nova" && fs.existsSync(configured)) return configured;
  if (configured && configured !== "nova") {
    // User pointed somewhere that doesn't exist — tell them plainly.
    const pick = await vscode.window.showWarningMessage(
      `Nova: binary not found at "${configured}".`,
      "Pick binary…", "Disable Nova LSP"
    );
    if (pick === "Pick binary…") return pickBinary();
    return null;
  }
  for (const c of ["nova", path.join(os.homedir(), ".nova", "bin", "nova"),
                   "/usr/local/bin/nova", "/opt/nova/bin/nova"]) {
    if (await onPath(c)) return c;
  }
  const pick = await vscode.window.showWarningMessage(
    "Nova: couldn't find the `nova` binary. Run/check/format need it; " +
    "LSP diagnostics are disabled until it's set.",
    "Pick binary…", "Install Nova…"
  );
  if (pick === "Pick binary…") return pickBinary();
  if (pick === "Install Nova…") vscode.env.openExternal(
    vscode.Uri.parse("https://sandeepvarma9902.github.io/nova-lang/download.html"));
  return null;
}

function onPath(cmd) {
  return new Promise((resolve) => {
    const checker = process.platform === "win32" ? "where" : "command";
    const args = process.platform === "win32" ? [cmd] : ["-v", cmd];
    execFile(checker, args, (err) => resolve(!err));
  });
}

async function pickBinary() {
  const uris = await vscode.window.showOpenDialog({
    canSelectFiles: true, canSelectMany: false,
    title: "Select the nova binary",
  });
  if (!uris || !uris.length) return null;
  const p = uris[0].fsPath;
  await cfg().update("serverPath", p, vscode.ConfigurationTarget.Global);
  return p;
}

function setStatus(state) {
  // state: "ok" | "off" | "error"
  if (!statusBar) {
    statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBar.command = "nova.restartServer";
    statusBar.show();
  }
  if (state === "ok") { statusBar.text = "$(check) Nova"; statusBar.tooltip = "Nova language server running — click to restart"; }
  else if (state === "off") { statusBar.text = "$(circle-slash) Nova"; statusBar.tooltip = "Nova LSP disabled — click for actions"; }
  else { statusBar.text = "$(warning) Nova"; statusBar.tooltip = "Nova language server isn't running — click to retry"; }
}

async function ensureBin() {
  if (!novaBin) novaBin = await resolveNovaBin();
  if (!novaBin) vscode.window.showWarningMessage("Nova: set the `nova.serverPath` setting to the nova binary first.");
  return novaBin;
}

function runInTerminal(cmd, args) {
  const term = vscode.window.createTerminal("Nova");
  term.show();
  const q = (s) => (s.includes(" ") ? `"${s}"` : s);
  term.sendText([cmd, ...args].map(q).join(" "));
}

function startLsp(context, bin) {
  const args = ["lsp", ...cfg().get("lsp.args", [])];
  const serverOptions = { command: bin, args, transport: TransportKind.stdio };
  const trace = cfg().get("trace.server", "off");
  const clientOptions = {
    documentSelector: [{ scheme: "file", language: "nova" }],
    synchronize: { configurationSection: "nova" },
    traceOutputChannel: trace === "off" ? undefined : vscode.window.createOutputChannel("Nova LSP trace"),
  };
  client = new LanguageClient("nova", "Nova Language Server", serverOptions, clientOptions);
  const { State } = require("vscode-languageclient/node");
  client.onDidChangeState(({ newState }) => {
    if (newState === State.Running) setStatus("ok");
    else if (newState === State.Stopped) {
      setStatus("error");
      out.appendLine("Nova language server stopped. Check `nova.serverPath`.");
    }
  });
  client.start().catch((e) => {
    setStatus("error");
    out.appendLine("Nova LSP failed to start: " + e.message);
  });
  context.subscriptions.push(client);
}

async function activate(context) {
  novaBin = await resolveNovaBin();

  if (novaBin && cfg().get("lsp.enabled", true)) {
    startLsp(context, novaBin);
  } else {
    setStatus(novaBin ? "off" : "error");
    if (novaBin) out.appendLine("Nova LSP disabled via `nova.lsp.enabled`.");
  }

  context.subscriptions.push(
    vscode.commands.registerCommand("nova.runFile", async () => {
      const bin = await ensureBin(); if (!bin) return;
      const doc = vscode.window.activeTextEditor?.document;
      if (!doc || doc.languageId !== "nova") {
        vscode.window.showWarningMessage("Nova: open a .nova file first.");
        return;
      }
      await doc.save();
      runInTerminal(bin, ["run", doc.fileName, ...cfg().get("run.args", [])]);
    }),
    vscode.commands.registerCommand("nova.checkFile", async () => {
      const bin = await ensureBin(); if (!bin) return;
      const doc = vscode.window.activeTextEditor?.document;
      if (!doc || doc.languageId !== "nova") {
        vscode.window.showWarningMessage("Nova: open a .nova file first.");
        return;
      }
      await doc.save();
      execFile(bin, ["check", doc.fileName], (err, stdout) => {
        out.show(true);
        out.appendLine(`$ nova check ${path.basename(doc.fileName)}`);
        let diags = [];
        try { diags = JSON.parse(stdout || "[]"); } catch { /* raw output below */ }
        if (!diags.length && !err) out.appendLine("✓ No problems found.");
        for (const d of diags) out.appendLine(`  ${d.code || ""} line ${d.line || "?"}: ${d.message || ""}`);
        if (!diags.length && stdout.trim()) out.appendLine(stdout.trim());
      });
    }),
    vscode.commands.registerCommand("nova.formatFile", async () => {
      const bin = await ensureBin(); if (!bin) return;
      const editor = vscode.window.activeTextEditor;
      const doc = editor?.document;
      if (!doc || doc.languageId !== "nova") {
        vscode.window.showWarningMessage("Nova: open a .nova file first.");
        return;
      }
      if (doc.isDirty) await doc.save();
      execFile(bin, ["fmt", doc.fileName], (err, stdout, stderr) => {
        if (err) {
          vscode.window.showErrorMessage("Nova: cannot format — " + (stderr || err.message).split("\n")[0]);
          return;
        }
        editor.edit((b) => b.replace(
          new vscode.Range(0, 0, doc.lineCount, 0), stdout));
      });
    }),
    vscode.commands.registerCommand("nova.restartServer", async () => {
      const bin = await ensureBin(); if (!bin) return;
      if (client) { await client.stop(); context.subscriptions.splice(context.subscriptions.indexOf(client), 1); }
      client = null;
      setStatus("off");
      if (cfg().get("lsp.enabled", true)) startLsp(context, bin);
    }),
    vscode.languages.registerDocumentFormattingEditProvider("nova", {
      provideDocumentFormattingEdits(doc) {
        return (async () => {
          if (!novaBin) return [];
          if (doc.isDirty) await doc.save();
          return new Promise((resolve) => {
            execFile(novaBin, ["fmt", doc.fileName], (err, stdout) => {
              if (err || !stdout) return resolve([]);
              resolve([vscode.TextEdit.replace(
                new vscode.Range(0, 0, doc.lineCount, 0), stdout)]);
            });
          });
        })();
      },
    })
  );
  if (statusBar) context.subscriptions.push(statusBar);
  context.subscriptions.push(out);
}

function deactivate() {
  if (!client) return undefined;
  return client.stop();
}

module.exports = { activate, deactivate };
