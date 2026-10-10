def classify(n: int) -> str:
    if n < 0:
        return "negative"
    if n == 0:
        return "zero"
    if n <= 9:
        return "small"
    return "big"


def sum_evens(xs: list[int]) -> int:
    total = 0
    for x in xs:
        if x % 2 == 0:
            total += x
    return total
