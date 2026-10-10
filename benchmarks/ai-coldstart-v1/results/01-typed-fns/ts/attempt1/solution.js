"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.classify = classify;
exports.sum_evens = sum_evens;
function classify(n) {
    if (n < 0)
        return "negative";
    if (n === 0)
        return "zero";
    if (n <= 9)
        return "small";
    return "big";
}
function sum_evens(xs) {
    let total = 0;
    for (const x of xs) {
        if (x % 2 === 0)
            total += x;
    }
    return total;
}
