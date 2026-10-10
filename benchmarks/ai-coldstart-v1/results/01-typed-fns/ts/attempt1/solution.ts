export function classify(n: number): string {
  if (n < 0) return "negative";
  if (n === 0) return "zero";
  if (n <= 9) return "small";
  return "big";
}

export function sum_evens(xs: number[]): number {
  let total = 0;
  for (const x of xs) {
    if (x % 2 === 0) total += x;
  }
  return total;
}
