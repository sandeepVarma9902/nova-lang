#!/bin/bash
# test.sh <solution-dir> — task 01-typed-fns
set -u
SOL="$1"
NOVA=~/workspace/nova-lang/nova
pass=0; fail=0
ok()   { pass=$((pass+1)); echo "ok - $1"; }
bad()  { fail=$((fail+1)); echo "FAIL - $1: $2"; }

if [ -f "$SOL/solution.nova" ]; then
  cat > "$SOL/driver.nova" <<'EOF'
import solution
effect fn main() {
  assert(solution.classify(-3) == "negative", "classify(-3)")
  assert(solution.classify(0) == "zero", "classify(0)")
  assert(solution.classify(5) == "small", "classify(5)")
  assert(solution.classify(9) == "small", "classify(9)")
  assert(solution.classify(10) == "big", "classify(10)")
  assert(solution.classify(99) == "big", "classify(99)")
  assert(solution.sum_evens([1, 2, 3, 4, 5]) == 6, "sum_evens basic")
  assert(solution.sum_evens([]) == 0, "sum_evens empty")
  assert(solution.sum_evens([1, 3, 5]) == 0, "sum_evens none")
  print("DRIVER OK")
}
main()
EOF
  out=$("$NOVA" run "$SOL/driver.nova" 2>&1); code=$?
  if [ $code -ne 0 ]; then bad "nova run" "$out"; else
    echo "$out" | grep -q "DRIVER OK" && ok "nova all assertions" || bad "nova asserts" "$out"
  fi
elif [ -f "$SOL/solution.ts" ]; then
  cat > "$SOL/driver.ts" <<'EOF'
import { classify, sum_evens } from "./solution";
function eq(a: unknown, b: unknown, name: string): void {
  if (a !== b) { throw new Error(`FAIL ${name}: got ${String(a)}`); }
}
eq(classify(-3), "negative", "c1"); eq(classify(0), "zero", "c2");
eq(classify(5), "small", "c3"); eq(classify(9), "small", "c4");
eq(classify(10), "big", "c5"); eq(classify(99), "big", "c6");
eq(sum_evens([1,2,3,4,5]), 6, "s1"); eq(sum_evens([]), 0, "s2");
eq(sum_evens([1,3,5]), 0, "s3");
console.log("DRIVER OK");
EOF
  (cd "$SOL" && tsc --strict --module commonjs --target es2020 solution.ts driver.ts 2>tsc.err)
  if [ -s "$SOL/tsc.err" ]; then bad "tsc --strict" "$(cat $SOL/tsc.err)"; else
    out=$(cd "$SOL" && node driver.js 2>&1); code=$?
    [ $code -eq 0 ] && echo "$out" | grep -q "DRIVER OK" && ok "ts all assertions" || bad "ts asserts" "$out"
  fi
elif [ -f "$SOL/solution.py" ]; then
  cat > "$SOL/driver.py" <<'EOF'
import solution
def eq(a, b, name):
    assert a == b, f"FAIL {name}: got {a!r}"
eq(solution.classify(-3), "negative", "c1"); eq(solution.classify(0), "zero", "c2")
eq(solution.classify(5), "small", "c3"); eq(solution.classify(9), "small", "c4")
eq(solution.classify(10), "big", "c5"); eq(solution.classify(99), "big", "c6")
eq(solution.sum_evens([1,2,3,4,5]), 6, "s1"); eq(solution.sum_evens([]), 0, "s2")
eq(solution.sum_evens([1,3,5]), 0, "s3")
print("DRIVER OK")
EOF
  out=$(cd "$SOL" && python3 driver.py 2>&1); code=$?
  [ $code -eq 0 ] && echo "$out" | grep -q "DRIVER OK" && ok "py all assertions" || bad "py asserts" "$out"
else
  bad "solution file" "no solution.nova / solution.ts / solution.py in $SOL"
fi

echo "---"
echo "pass=$pass fail=$fail"
[ $fail -eq 0 ] && [ $pass -gt 0 ]
