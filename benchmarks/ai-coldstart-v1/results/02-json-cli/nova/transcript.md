# Transcript — 02-json-cli / nova

Docs read: spec.md + cheatsheets/nova.txt (ai-build.txt).

## Attempt 1 — FAIL (compile)

Wrote the straightforward thing: `effect io fn main(io: IO)` with
`match` arms as `{ }` blocks containing prints + bare `return`.

Compiler: `E_SYNTAX: unexpected { (line 4, col 15), hint "expected an
expression"` — match arms must be single expressions, not statement blocks.

## Attempt 2 — FAIL (compile)

Restructured toward `?` + Result-returning helpers, but still used `{ }`
blocks as match arms in two places.

Compiler: `E_SYNTAX: unexpected { (line 43)` — same rule, new location.

## Attempt 3 — FAIL (compile)

Eliminated match-in-loop entirely: added `toUserList(item: Json) ->
List[User]` helper (Some → `[u]`, None → `[]`), nested `for` loops with
`push`. No match arms contain statements anymore.

Compiler: `E_RETURN: function 'toUser' may exit without returning
Option[User], hint "end every path with return <value>"` — Nova does not
implicitly return the tail expression; `return` must be explicit.

## Attempt 4 — FAIL (compile)

Wrapped both tail matches in explicit `return`.

Compiler: `E_TYPE_MISMATCH: operator * needs Int/Int, Float/Float or
String+String, got Int and Float, hint "Nova has no implicit conversions"`
on `ageSum * 1.0`. There is no Int→Float conversion builtin; the only path
is `parseFloat(str(n))`, which returns `Option[Float]`.

## Attempt 5 — GREEN

Added `fn toFloat(n: Int) -> Float` via `parseFloat` + unwrap. All four
test cases pass (valid, sparse, missing file, malformed JSON).

Note: the error cases pass via `assert(false, msg)` in `main` — a Nova
`NovaError` exits 1 and writes the message inside a JSON diagnostic on
stderr, which satisfies the test's grep + nonzero-exit checks. There is no
`exit`/`eprint` builtin in v1; this workaround was discovered through the
test failures, not from the docs.

Result: GREEN in 5 attempts.
