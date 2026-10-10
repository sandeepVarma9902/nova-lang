#!/bin/bash
# test.sh <solution-dir> — task 03-rest-api
set -u
SOL="$1"
NOVA=~/workspace/nova-lang/nova
PORT=8931
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok - $1"; }
bad() { fail=$((fail+1)); echo "FAIL - $1: $2"; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"; kill $SRVPID 2>/dev/null' EXIT
SRVPID=""

if [ -f "$SOL/solution.nova" ]; then
  cp "$SOL/solution.nova" "$WORK/"
  ("$NOVA" run "$WORK/solution.nova" >"$WORK/server.log" 2>&1 & echo $! > "$WORK/pid")
elif [ -f "$SOL/solution.ts" ]; then
  cp "$SOL/solution.ts" "$WORK/"
  if [ ! -d "$(dirname "$0")/node_modules" ]; then
    (cd "$(dirname "$0")" && npm init -y >/dev/null 2>&1 && npm install express @types/express --no-audit --no-fund 2>&1 | tail -1)
  fi
  (cd "$WORK" && NODE_PATH="$(dirname "$0")/node_modules" tsc --strict --module commonjs --target es2020 --moduleResolution node --esModuleInterop solution.ts 2>tsc.err)
  [ -s "$WORK/tsc.err" ] && { bad "tsc --strict" "$(cat $WORK/tsc.err)"; echo "pass=$pass fail=$fail"; exit 1; }
  (cd "$WORK" && NODE_PATH="$(dirname "$0")/node_modules" node solution.js >"$WORK/server.log" 2>&1 & echo $! > "$WORK/pid")
elif [ -f "$SOL/solution.py" ]; then
  cp "$SOL/solution.py" "$WORK/"
  (cd "$WORK" && python3 solution.py >"$WORK/server.log" 2>&1 & echo $! > "$WORK/pid")
else
  bad "solution file" "no solution.nova / solution.ts / solution.py in $SOL"
  echo "pass=$pass fail=$fail"; exit 1
fi
SRVPID=$(cat "$WORK/pid")

# wait for port
ready=0
for _ in $(seq 1 50); do
  if (echo > /dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then ready=1; break; fi
  sleep 0.2
done
[ $ready -eq 0 ] && { bad "server start" "port $PORT not open after 10s; log: $(cat $WORK/server.log | head -5)"; echo "pass=$pass fail=$fail"; exit 1; }
ok "server start"

check() { # $1=desc $2=path $3=want-code $4=want-substr
  local body code
  body=$(curl -s -w '\n%{http_code}' "http://127.0.0.1:$PORT$2")
  code=$(echo "$body" | tail -1); body=$(echo "$body" | head -n -1)
  local ctype
  ctype=$(curl -s -o /dev/null -D - "http://127.0.0.1:$PORT$2" | grep -i "^content-type:" | tr -d '\r')
  if [ "$code" = "$3" ] && echo "$body" | grep -q "$4" && echo "$ctype" | grep -qi "application/json"; then
    ok "$1"
  else
    bad "$1" "want $3 + [$4] + json ctype; got $code body=[$body] ctype=[$ctype]"
  fi
}

check "GET /users/1" "/users/1" "200" '"Ada"'
check "GET /users/2" "/users/2" "200" '"Ben"'
check "GET /users/999" "/users/999" "404" 'error'
check "GET /users/abc" "/users/abc" "400" 'error'
check "GET /users/0" "/users/0" "400" 'error'

echo "---"
echo "pass=$pass fail=$fail"
[ $fail -eq 0 ] && [ $pass -gt 0 ]
