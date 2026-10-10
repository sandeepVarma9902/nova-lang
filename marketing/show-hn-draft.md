# Show HN draft — Nova (for Sandeep's review; he posts from his HN account)

Title: Show HN: Nova – Java's discipline × React's soul, a language written for AI

Body:

Hi HN. I built Nova, a programming language that takes Java's static
discipline (types, modules, portability) and React's component model
(components, declarative views, compiler-managed state) and fuses them
into one language — designed from day one to be written fluently by AI
coding agents as well as humans.

Why: Java gives you safety but drowns you in boilerplate and null.
React gives you components but hooks have rules, types are optional,
and the toolchain is a project of its own. Nova keeps the good parts
and drops the rest:

- No null — Option[T] and Result[T,E] with the ? operator
- Components with type-checked views and immutable props; state is
  compiler-managed, so there are no hook rules to break
- Pure-by-default functions; effects and IO capabilities are explicit
- Every compiler error is a stable code with a JSON-readable fix hint,
  so an AI agent can parse a diagnostic and repair code mechanically
- One toolchain: `nova build`, `nova deploy`, bytecode VM, built-in web
  framework (Nova Boot), LSP + VS Code extension

Status is honest: the reference implementation (interpreter + bytecode
VM, stdlib-only) is real and everything in the tour runs. It's not
faster than Java yet — a native runtime is the next milestone, and the
performance doc says exactly where we stand.

Try it in the browser, no install: [playground link]
Docs: [docs link]  GitHub: [repo link]

I'd genuinely like critique, especially on the component/type-system
design. What would you steal, and what would you kill?

---

Posting notes:
- Post from Sandeep's HN account, morning US time for visibility.
- Reply to every substantive comment within the first few hours.
- Do NOT seed fake comments or upvotes.
