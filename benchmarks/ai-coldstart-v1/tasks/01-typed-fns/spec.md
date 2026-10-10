# Task 01 — typed-fns

Write a module/file named `solution` (`solution.nova` / `solution.ts` /
`solution.py`) with two functions:

1. `classify(n)` — takes an integer, returns a string:
   - `n < 0` → `"negative"`
   - `n == 0` → `"zero"`
   - `1 <= n <= 9` → `"small"`
   - `n >= 10` → `"big"`
2. `sum_evens(xs)` — takes a list/array of integers, returns the sum of the
   even ones (0 for an empty list).

Use explicit parameter and return types on both functions (all three languages
support/require annotations — write them).

Do NOT write a main program or print anything. Just the two functions. They
must be importable from another file in the same directory.
