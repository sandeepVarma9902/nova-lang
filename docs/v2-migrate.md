# `nova migrate` — mechanical Java/JSX → Nova migration (v2)

> **EXPERIMENTAL.** `nova migrate --from java|jsx <file>` converts the most
> mechanical subset of Java / React-JSX to Nova. It is a mapping assistant,
> not a transpiler: every construct it cannot convert is preserved as a
> `// UNCONVERTED` / `// WARNING` comment **and** reported as a warning with
> a source line number (stdout carries the Nova code, stderr carries the
> warnings, and a warnings summary is appended to the file itself). Nothing
> is silently dropped. Review every line before you `nova check` it.
>
> The conceptual mappings live in [migration-guide.md](./migration-guide.md)
> (the v1 source of truth). This document describes exactly what the *tool*
> does mechanically, what it warns about, and where it stops.

Workflow: `nova migrate --from java Foo.java > Foo.nova` → `nova fmt` →
`nova check` → work through each diagnostic and each `// WARNING` →
`nova lock` when imports settle.

---

## Table A — Java → Nova (mechanical subset)

| Java | Nova | Notes |
|---|---|---|
| `class C { ... }` | `class C { ... }` | Same shape |
| `interface I { ... }` | `interface I { ... }` | Signatures as `fn`; `default` methods warn |
| `record R(...)` / pure bean (fields + ctor + getters/setters) | `data class R(...)` | Getters/setters/`equals`/`hashCode`/`toString` folded away (warned) |
| bean **with setters** | `class` with `var` fields + `init` | Data classes are immutable; mutable beans stay classes |
| constructor `C(...)` | `init(...)` | `super(...)` preserved; `this(...)` chaining warns |
| `extends B` / `implements I, J` | `extends B` / `implements I, J` | Generics erased with a warning |
| getters `getX()` / `isX()` | folded into the field | Only when a matching field exists |
| setters `setX(v)` | folded → `var` field | Warned; data-class path not taken |
| calls `o.getX()` / `o.setX(v)` | `o.x` / `o.x = v` | Rewritten wherever the accessor was folded |
| `@Override` | `override` | Other annotations warn + drop |
| `static` method / field | module-level `fn` / `let` | Warned ("hoisted") |
| `int/long/short/byte`, wrappers | `Int` | `long` suffix stripped (warned) |
| `double/float`, wrappers | `Float` | `f` suffix stripped (warned) |
| `String`, `boolean`, `Object`, `char` | `String`, `Bool`, `Any`, `String` | `char` literals become `"c"` (warned) |
| `List<X>` / `X[]` / `ArrayList<X>` | `List[X]` | `Set<X>` → `List[X]` + warning (dedup lost) |
| `Optional<X>` | `Option[X]` | `Optional.of/empty` → `Some/None` |
| `Map<K,V>` and friends | `Any` | **Warning** — no Nova equivalent; model by hand |
| other generics `Foo<Bar>` | `Any` | **Warning** — generics erased |
| unknown types | `Any` | **Warning** — annotate by hand |
| `int x = e;` (local) | `var x: Int = e` | `final` → `let`; no-init → zero value + warning |
| `x++` / `x--` / `x op= e` | `x = x + 1` / `x = x - 1` / `x = x op (e)` | |
| `if (c)` / `else if` / `else` | `if c` / `else if c` / `else` | |
| `while (c)` | `while c` | |
| `do { } while (c);` | `while true { ...; if not (c) { break } }` | **Warning** — verify |
| `for (T x : xs)` | `for x in xs` | |
| `for (init; c; upd)` | `init` + `while c { ...; upd }` | **Warning** — bounds made explicit, verify |
| `switch` | `if/else-if` chain on `==` | **Warning**; fall-through flagged; stacked cases merge with `or` |
| `x == null` / `x != null` | commented `match` skeleton + original preserved | **Warning** — model as `Option`, Recipe 4 |
| `try { } catch (E e) { }` | try-body kept; handler as comments | **Warning** — rewrite as `Result` + `?` + `match`, Recipe 2 |
| `throw new E(msg)` in a `throws` method | `return Err(msg)`; method → `-> Result[T, String]` | **Warning** — error type flattened, pick a real one |
| `throw` elsewhere | `// UNCONVERTED` | **Warning** — Nova has no exceptions |
| `synchronized` | body kept, lock dropped | **Warning** |
| `System.out.println(x)` | `print(x)` | Method guessed `effect` (warned — review) |
| `new Foo(a)` | `Foo(a)` | |
| `new ArrayList<>()` | `[]` | Copy-construction warns |
| `&&` / `\|\|` / `!x` | `and` / `or` / `not x` | |
| `c ? a : b` | `if c { a } else { b }` | |
| `s.length()` / `l.size()` / `a.length` | `len(s)` / `len(l)` / `len(a)` | |
| `l.add(x)` / `l.get(i)` | `l.push(x)` / `l[i]` | For known `List` vars; otherwise warned |
| `a.equals(b)` | `(a == b)` | |
| `x.toString()` / `String.valueOf(x)` | `str(x)` | |
| `Integer.parseInt` / `Long.parseLong` / `Double.parseDouble` | `int(...)` / `int(...)` / `float(...)` | |
| `Boolean.parseBoolean(s)` | `(s == "true")` | Warned |
| `(Type) x` casts | `x` | **Warning** — Nova has no casts |
| `import java.util.List` etc. | *(dropped)* | Built in; correct, no warning |
| `import com.foo.Bar` | `import bar` | **Warning** — VERIFY the module mapping |
| `import static ...` / wildcard `.*` | dropped | **Warning** |
| nested classes | hoisted to top level | **Warning** — verify scoping |
| `enum` | `// UNCONVERTED` | **Warning** — model as data class + constants |

## Table B — JSX → Nova (mechanical subset)

| JSX | Nova | Notes |
|---|---|---|
| `function C({...})` / `const C = (...) =>` | `component C(...)` | Capitalized name = component (React convention) |
| destructured props `{a, b}` | params `a: Any, b: Any` | **Warning per prop** — untyped JS, annotate |
| `(props)` + `props.x` uses | params harvested from uses | **Warning** — annotate types |
| prop defaults `{a = 1}` | dropped | **Warning** — Nova has no default params |
| `const [x, setX] = useState(v)` | `state x: T = v` | `T` inferred from literals; `[]` → `List[Any]` + warning; `{}` → warning (model as data class) |
| `setX(e)` | `x = e` | |
| `setX(p => expr)` | `x = expr[p:=x]` | Updater param substituted |
| `arr.map(x => <li>...</li>)` | `<for x in arr>...</for>` | `key=` dropped + warning; parenthesized callbacks `p => (<div/>)` handled |
| `{cond && <A/>}` | `<if cond>` | Complex conditions → `<if cond={...}>` + warning |
| `{c ? <A/> : <B/>}` | two `<if>` blocks | **Warning** — Nova `<if>` has no else; verify |
| `{c ? a : b}` (values) | `{if c { a } else { b }}` | Nova if-expressions |
| `onClick={() => setX(...)}` | `onClick={x = ...}` | Handlers are single expressions |
| `const h = (p) => ...` + `onClick={() => h(x)}` | handler body inlined with args substituted | Template consts are not emitted separately |
| `setX([...x, e])` | `x.push(e)` | Append-pattern only; other spreads warn + drop |
| `const C = ({t}) => <h1>{t}</h1>` | `component C(t: Any)` | Expression-bodied arrows supported |
| `onClick={handler}` | `onClick={handler()}` | **Warning** — bare ref emitted as call; verify |
| arrow with `(e)` param | param dropped | **Warning** — no event object in Nova |
| multi-statement handler | attribute dropped | **Warning** — extract to an `effect fn` |
| `className=` | `class=` | `htmlFor` → `for` |
| `{expr}` | `{expr}` | `===`→`==`, `&&`→`and`, template literals → `"a" + str(b)` |
| `const x = <expr>` in body | inlined at `{x}` uses in the view | **Warning** — verify |
| `const h = () => ...` in body | inlined at `onX={h}` | Handler template |
| plain `function f()` (lowercase) | module-level `fn f(...)` | Params `Any` + warning |
| `import React` / `react-dom` | *(dropped)* | The framework; correct, no warning |
| `import './x.css'` | *(dropped)* | **Warning** — Nova views have no stylesheets |
| `import X from './X'` | `import x` | **Warning** — VERIFY the module mapping |
| `import axios from 'axios'` | *(dropped)* | **Warning** — no Nova equivalent |

---

## Warning catalog (every warning points at a source line)

**Java**
- `type 'X' has no Nova mapping — emitted as Any` · `generics on 'X' are erased`
- `Map has no Nova equivalent (v1)` · `Set -> List (dedup semantics lost)`
- `null check on 'x' — model as Option and handle with match`
- `'throw' -> 'return Err(...)'` / `checked exception 'E' flattened to Result[_, String]`
- `try/catch -> Result[_, String] + '?' + match` · `'finally' has no Nova equivalent`
- `C-style for -> while loop` · `'do/while' rewritten as 'while true'`
- `'switch' -> if/else-if chain` · `switch fall-through in case ...`
- `'synchronized' dropped` · `cast '(T)' dropped` · `'instanceof' has no Nova equivalent`
- `lambda '->' — Nova has no first-class functions` · `method reference '::'`
- `static members of 'C' hoisted to module level`
- `multiple constructors — Nova has no overloading; only the first is converted`
- `constructor chaining 'this(...)'` · `abstract ... — Nova has no abstract`
- `import 'com.foo.Bar' -> 'import bar' — VERIFY the module mapping`
- `'enum X' — Nova has no enums` · `nested class 'I' hoisted to top level`
- `no initializer for 'x' — Nova requires one; zero value ... used`
- `method 'm' marked 'effect' (calls print) — review effect placement`

**JSX**
- `prop 'x' is untyped (JS) — emitted as Any` · `props object 'p' destructured into params`
- `default value for prop 'x' dropped — Nova has no default params`
- `useState([]) — element type unknown` · `object-literal state — model as a data class`
- `'useEffect' — no direct equivalent` (cleanup especially) · `'useRef'/'useMemo'/... — no Nova equivalent`
- `JSX ternary -> two <if> blocks` · `complex <if> condition ... emitted as cond={...}`
- `'.map()' with a key= attribute drops the key`
- `event parameter(s) ... dropped — Nova handlers receive no event object`
- `multi-statement event handler — ... extract to an effect fn`
- `'style={{...}}' (CSS-in-JS) dropped` · `spread attributes '{...p}' dropped`
- `'dangerouslySetInnerHTML' dropped`
- `external package 'x' has no Nova equivalent` · `stylesheet import ... dropped`
- `'x' is async — Nova's async model differs; 'await' dropped`
- `'?.' optional chaining -> plain '.'` · `'??' — right side dropped; use match on Option`
- `spread '...' has no Nova equivalent — dropped`
- `arrow function '=>' — Nova has no first-class functions`

---

## Honest limits (what the tool will never do for you)

1. **Effects and capabilities.** `print` presence guesses `effect`; everything
   else is unmarked `fn`. Expect `E_EFFECT` on first `nova check` — annotate
   deliberately. `effect io`/`net` functions need their `IO`/`Net` parameter
   threaded from the entry module; that wiring is a design decision.
2. **Error types.** Checked exceptions flatten to `Result[T, String]`. Pick a
   real error type per call chain.
3. **Nullability.** `x != null` becomes a commented recipe, not code — only
   you know which bindings are `Option[T]`.
4. **Generics.** Everything beyond `List`/`Option` is erased to `Any`.
5. **No semantic understanding.** Name shadowing, overload resolution,
   dynamic dispatch subtleties, and reflection-driven behavior are invisible
   to a line/brace mapper. The output parses; "parses" is not "correct".
6. **JSX is untyped.** Every prop and helper param arrives as `Any`. The
   warnings tell you exactly which lines to annotate.
7. **Styling and DOM.** CSS, inline styles, refs, event objects, and
   `dangerouslySetInnerHTML` have no Nova counterpart — they are dropped
   loudly, never silently.

A full transpiler is future work (see ROADMAP.md). Until then: this tool
does the typing, you do the thinking.
