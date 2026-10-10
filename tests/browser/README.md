# Browser e2e harness (real Pyodide, no threads)

`e2e_pyodide.js` loads REAL Pyodide in Node, writes the 25 vendored
`website/nova_py/*.py` files via `pyodide.FS`, extracts the EXACT harness from
`website/js/nova-live.js`, and runs every runnable snippet from
`website/docs/*.html` + `website/js/examples.json`.

`--nothreads` patches `threading.Thread.start` to raise RuntimeError, simulating
the GitHub Pages browser environment (no COOP/COEP headers -> no OS threads).

Usage (from repo root):

```bash
cd tests/browser && npm install   # installs pyodide (once)
node e2e_pyodide.js --nothreads
```

**Zero failures required before any website deploy.** This is the strongest
pre-deploy proof — it caught the sqlite3 stdlib gap that the pure-Python sim
could not.
