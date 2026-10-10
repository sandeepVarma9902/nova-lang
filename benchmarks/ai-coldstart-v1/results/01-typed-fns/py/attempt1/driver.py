import solution
def eq(a, b, name):
    assert a == b, f"FAIL {name}: got {a!r}"
eq(solution.classify(-3), "negative", "c1"); eq(solution.classify(0), "zero", "c2")
eq(solution.classify(5), "small", "c3"); eq(solution.classify(9), "small", "c4")
eq(solution.classify(10), "big", "c5"); eq(solution.classify(99), "big", "c6")
eq(solution.sum_evens([1,2,3,4,5]), 6, "s1"); eq(solution.sum_evens([]), 0, "s2")
eq(solution.sum_evens([1,3,5]), 0, "s3")
print("DRIVER OK")
