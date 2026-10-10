import { readFileSync } from "fs";

interface User { name: string; age: number; email?: string }

function isValid(u: unknown): u is User {
  if (typeof u !== "object" || u === null) return false;
  const o = u as Record<string, unknown>;
  return typeof o["name"] === "string" && typeof o["age"] === "number";
}

function main(): void {
  let raw: string;
  try {
    raw = readFileSync("data.json", "utf8");
  } catch {
    console.error("error: data.json not found");
    process.exit(1);
  }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    console.error("error: malformed JSON");
    process.exit(1);
  }
  if (!Array.isArray(data)) {
    console.error("error: malformed JSON");
    process.exit(1);
  }
  const users = data.filter(isValid);
  const count = users.length;
  const ageSum = users.reduce((s, u) => s + u.age, 0);
  const avg = count === 0 ? 0 : ageSum / count;
  const noEmail = users.filter((u) => u.email === undefined).map((u) => u.name);
  console.log(`users: ${count}`);
  console.log(`avg_age: ${avg.toFixed(1)}`);
  console.log(`no_email: ${noEmail.length === 0 ? "none" : noEmail.join(", ")}`);
}

main();
