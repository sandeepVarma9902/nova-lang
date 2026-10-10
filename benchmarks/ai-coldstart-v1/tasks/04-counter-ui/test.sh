#!/bin/bash
# test.sh <solution-dir> — task 04-counter-ui
set -u
SOL="$1"
NOVA=~/workspace/nova-lang/nova
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok - $1"; }
bad() { fail=$((fail+1)); echo "FAIL - $1: $2"; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if [ -f "$SOL/solution.nova" ]; then
  cp "$SOL/solution.nova" "$WORK/"
  out=$("$NOVA" run "$WORK/solution.nova" 2>&1); code=$?
elif [ -f "$SOL/solution.ts" ]; then
  cp "$SOL/solution.ts" "$WORK/"
  (cd "$WORK" && tsc --strict --module commonjs --target es2020 solution.ts 2>tsc.err)
  [ -s "$WORK/tsc.err" ] && { bad "tsc --strict" "$(cat $WORK/tsc.err)"; echo "pass=$pass fail=$fail"; exit 1; }
  out=$(cd "$WORK" && node solution.js 2>&1); code=$?
elif [ -f "$SOL/solution.py" ]; then
  cp "$SOL/solution.py" "$WORK/"
  out=$(cd "$WORK" && python3 solution.py 2>&1); code=$?
else
  bad "solution file" "no solution.nova / solution.ts / solution.py in $SOL"
  echo "pass=$pass fail=$fail"; exit 1
fi

[ $code -ne 0 ] && { bad "run" "exit=$code output=[$out]"; echo "pass=$pass fail=$fail"; exit 1; }

echo "$out" | grep -q "Clicks"     && ok "title rendered"      || bad "title" "no 'Clicks' in [$out]"
echo "$out" | grep -q "Increment"  && ok "button rendered"     || bad "button" "no 'Increment' in [$out]"
# count 0 before click, 1 after: look for >0< and >1< in the html lines
n0=$(echo "$out" | grep -c ">0<"); n1=$(echo "$out" | grep -c ">1<")
# fall back: quoted "0"/"1" on their own line (Nova pretty-print)
[ "$n0" -eq 0 ] && n0=$(echo "$out" | grep -c '^\s*"0"\s*$')
[ "$n1" -eq 0 ] && n1=$(echo "$out" | grep -c '^\s*"1"\s*$')
[ "$n0" -ge 1 ] && ok "count 0 shown" || bad "count 0" "not found in [$out]"
[ "$n1" -ge 1 ] && ok "count 1 shown" || bad "count 1" "not found in [$out]"

echo "---"
echo "pass=$pass fail=$fail"
[ $fail -eq 0 ] && [ $pass -gt 0 ]
