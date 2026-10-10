# Task 04 — counter-ui

Build a counter with a title.

Requirements:

- It is created with a title string, e.g. `"Clicks"`.
- It holds a count starting at `0`.
- It can render itself to an HTML string containing: the title, the current
  count, and a button labeled `Increment`.
- It has a way to simulate a button click: after one simulated click the count
  is `1`, and rendering again shows `1`.

Use each language's natural construct:

- **Nova**: a `component` with a `view`, `render(...)` it, and
  `app.fire("onClick")` to simulate the click. Print the rendered HTML before
  and after the click.
- **TypeScript**: a class with `render(): string` and `click(): void`.
  Print `render()` before and after `click()`.
- **Python**: a class with `render()` and `click()`. Print `render()` before
  and after `click()`.

The test runs your program and checks the output shows the title, the count
going `0` → `1`, and an `Increment` button.
