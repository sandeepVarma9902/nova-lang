#!/usr/bin/env python3
# test_db.py — database nativity: pull codegen, typed queries, and
# check-time schema validation (E_DB_SCHEMA).

import json
import os
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOVA = os.path.join(ROOT, "nova")


def run(args, **kw):
    return subprocess.run([NOVA] + args, capture_output=True, text=True,
                          timeout=60, **kw)


def main():
    tmp = tempfile.mkdtemp(prefix="nova-db-test-")
    db_path = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
                 "name TEXT NOT NULL, price REAL, tags TEXT)")
    conn.execute("INSERT INTO widgets VALUES (1, 'sprocket', 9.5, 'a'), "
                 "(2, 'cog', 2.0, NULL)")
    conn.commit()
    conn.close()

    failures = []

    def check(name, cond, detail=""):
        if cond:
            print("  ok %s" % name)
        else:
            failures.append("%s %s" % (name, detail))

    # 1. db pull generates models + schema
    out = os.path.join(tmp, "models.nova")
    p = run(["db", "pull", "--dsn", "sqlite:///" + db_path, "--out", out])
    check("pull exit 0", p.returncode == 0, p.stderr)
    src = open(out).read()
    check("pull data class",
          "data class Widget(id: Int, name: String, price: Option[Float], "
          "tags: Option[String])" in src, src[:200])
    check("pull fromJson", "fn widgetFromJson(j: Json)" in src)
    schema_path = os.path.join(tmp, ".nova-schema.json")
    check("pull schema json", os.path.isfile(schema_path))
    schema = json.load(open(schema_path))
    check("schema tables", schema["tables"]["widgets"]["name"] == "String",
          str(schema))

    # generated models check clean
    p = run(["check", out])
    check("models check clean", p.returncode == 0, p.stdout[:500])

    # 2. typed query runs end to end
    prog = r'''
import models

effect io fn q(cap: IO) -> Result[String, String] {
  let db = dbOpen("sqlite:///{DB}", cap)?
  let rows = dbTable(db, "widgets").where("price", ">", 5.0).all()?
  let names: List[String] = []
  for row in rows {
    let w = match models.widgetFromJson(row) {
      Ok(x) => Ok(x),
      Err(e) => Err(e)
    }?
    names.push(w.name)
  }
  return Ok(names[0])
}

match q(io) {
  Ok(n) => print(n),
  Err(e) => print("ERR " + e)
}
'''.replace("{DB}", db_path)
    prog_path = os.path.join(tmp, "q.nova")
    open(prog_path, "w").write(prog)
    # models.nova sits next to the program, imported as `models`
    p = run(["run", prog_path])
    check("query run", p.returncode == 0 and p.stdout.strip() == "sprocket",
          "rc=%d out=%r err=%r" % (p.returncode, p.stdout, p.stderr[:300]))

    # 3. check-time schema validation (best effort, literal names)
    bad = r'''
effect io fn q(cap: IO) -> Result[String, String] {
  let db = dbOpen("sqlite:///{DB}", cap)?
  let q = dbTable(db, "widgetz")
  let q2 = dbTable(db, "widgets").where("pric", "=", 1)
  return Ok("x")
}
q(io)
'''.replace("{DB}", db_path)
    bad_path = os.path.join(tmp, "bad.nova")
    open(bad_path, "w").write(bad)
    p = run(["check", bad_path])
    diags = json.loads(p.stdout) if p.stdout.strip() else []
    codes = [d["code"] for d in diags]
    check("E_DB_SCHEMA table", "E_DB_SCHEMA" in codes, str(codes))
    check("E_DB_SCHEMA column", codes.count("E_DB_SCHEMA") >= 2, str(codes))

    # 4. honest errors for other drivers
    p = run(["db", "pull", "--dsn", "postgres://u@h/db",
             "--out", os.path.join(tmp, "x.nova")])
    check("postgres honest", p.returncode != 0 and "driver" in p.stderr,
          p.stderr[:200])

    # 5. data export
    p = run(["data", "export", "--from", "sqlite:///" + db_path,
             "--query", "SELECT id, name FROM widgets ORDER BY id",
             "--format", "jsonl"])
    lines = p.stdout.strip().split("\n")
    check("export jsonl", p.returncode == 0 and len(lines) == 2
          and json.loads(lines[0]) == {"id": 1, "name": "sprocket"},
          p.stdout[:200] + p.stderr[:200])

    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("db: all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
