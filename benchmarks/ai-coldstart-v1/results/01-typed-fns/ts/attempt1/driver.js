"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const solution_1 = require("./solution");
function eq(a, b, name) {
    if (a !== b) {
        throw new Error(`FAIL ${name}: got ${String(a)}`);
    }
}
eq((0, solution_1.classify)(-3), "negative", "c1");
eq((0, solution_1.classify)(0), "zero", "c2");
eq((0, solution_1.classify)(5), "small", "c3");
eq((0, solution_1.classify)(9), "small", "c4");
eq((0, solution_1.classify)(10), "big", "c5");
eq((0, solution_1.classify)(99), "big", "c6");
eq((0, solution_1.sum_evens)([1, 2, 3, 4, 5]), 6, "s1");
eq((0, solution_1.sum_evens)([]), 0, "s2");
eq((0, solution_1.sum_evens)([1, 3, 5]), 0, "s3");
console.log("DRIVER OK");
