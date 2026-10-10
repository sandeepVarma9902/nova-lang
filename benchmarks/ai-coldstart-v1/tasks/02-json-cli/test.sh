#!/bin/bash
# test.sh <solution-dir> — task 02-json-cli
set -u
SOL="$1"
FIX="$(cd "$(dirname "$0")/fixtures" && pwd)"
NOVA=~/workspace/nova-lang/nova
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok - $1"; }
bad() { fail=$((fail+1)); echo "FAIL - $1: $2"; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ---- build the runnable command per language ----
if [ -f "$SOL/solution.nova" ]; then
  cp "$SOL/solution.nova" "$WORK/"
  RUN=("$NOVA" run "$WORK/solution.nova")
elif [ -f "$SOL/solution.ts" ]; then
  cp "$SOL/solution.ts" "$WORK/"
  (cd "$WORK" && npm init -y >/dev/null 2>&1 && npm install --no-audit --no-fund @types/node >/dev/null 2>&1; tsc --strict --types node --module commonjs --target es2020 solution.ts 2>tsc.err)
  [ -s "$WORK/tsc.err" ] && { bad "tsc --strict" "$(cat $WORK/tsc.err)"; echo "pass=$pass fail=$fail"; exit 1; }
  RUN=(node "$WORK/solution.js")
elif [ -f "$SOL/solution.py" ]; then
  cp "$SOL/solution.py" "$WORK/"
  RUN=(python3 "$WORK/solution.py")
else
  bad "solution file" "no solution.nova / solution.ts / solution.py in $SOL"
  echo "pass=$pass fail=$fail"; exit 1
fi

run_case() { # $1=fixture-or-MISSING $2=desc
  rm -f "$WORK/data.json"
  [ "$1" != "MISSING" ] && cp "$FIX/$1" "$WORK/data.json"
  (cd "$WORK" && "${RUN[@]}" >stdout.txt 2>stderr.txt); echo $? > "$WORK/code.txt"
}

check_out() { # $1=desc $2=expected-stdout $3=expected-code
  local so; so=$(cat "$WORK/stdout.txt")
  local code; code=$(cat "$WORK/code.txt")
  if [ "$so" = "$2" ] && [ "$code" = "$3" ]; then ok "$1"; else
    bad "$1" "want stdout=[$2] code=$3 got stdout=[$so] code=$code stderr=[$(cat $WORK/stderr.txt)]"
  fi
}

run_case valid.json "valid"
check_out "valid.json" "$(printf 'users: 4\navg_age: 31.5\nno_email: Ben, Dan')" "0"

run_case sparse.json "sparse"
check_out "sparse.json" "$(printf 'users: 2\navg_age: 26.0\nno_email: Ada')" "0"

run_case MISSING "missing file"
code=$(cat "$WORK/code.txt")
if [ "$code" != "0" ] && grep -q "error: data.json not found" "$WORK/stderr.txt"; then ok "missing file"; else
  bad "missing file" "want nonzero + 'error: data.json not found' on stderr, got code=$code stderr=[$(cat $WORK/stderr.txt)]"
fi

run_case malformed.json "malformed"
code=$(cat "$WORK/code.txt")
if [ "$code" != "0" ] && grep -q "error: malformed JSON" "$WORK/stderr.txt"; then ok "malformed json"; else
  bad "malformed json" "want nonzero + 'error: malformed JSON' on stderr, got code=$code stderr=[$(cat $WORK/stderr.txt)]"
fi

echo "---"
echo "pass=$pass fail=$fail"
[ $fail -eq 0 ] && [ $pass -gt 0 ]
