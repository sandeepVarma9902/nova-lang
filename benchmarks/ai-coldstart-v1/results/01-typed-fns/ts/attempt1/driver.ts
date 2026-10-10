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
