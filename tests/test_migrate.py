#!/usr/bin/env python3
# test_migrate.py — tests for `nova migrate --from java|jsx` (EXPERIMENTAL).
#
# For every fixture the test asserts three things:
#   1. the Nova output contains the expected mechanical mapping,
#   2. warnings fire (with source line numbers) for the non-mechanical bits,
#   3. the output parses with ZERO errors through Nova's own parser.
#
# Run: python3 tests/test_migrate.py   (exit 0 = all green)

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nova_lang.migrate import migrate
from nova_lang.parser import parse_source, ParseError, JsxError

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("ok   %s" % name)
    else:
        FAILURES.append(name)
        print("FAIL %s" % name)
        if detail:
            print(detail)


def convert(src, flavor, filename):
    out, warnings = migrate(src, filename, flavor)
    return out, warnings


def assert_parses(name, out):
    try:
        parse_source(out, "<migrated-%s>" % name)
        check("%s parses with zero errors" % name, True)
    except (ParseError, JsxError) as e:
        check("%s parses with zero errors" % name, False,
              "  parse error: %s\n  --- output ---\n%s" % (e, out))


def assert_in(name, out, needle):
    check("%s emits %r" % (name, needle[:50]), needle in out,
          "  missing %r\n  --- output ---\n%s" % (needle, out))


def assert_not_in(name, out, needle):
    check("%s drops %r" % (name, needle[:50]), needle not in out,
          "  unexpectedly present: %r" % needle)


def assert_warn(name, warnings, needle):
    check("%s warns %r" % (name, needle[:50]),
          any(needle in w for w in warnings),
          "  no warning containing %r\n  --- warnings ---\n%s"
          % (needle, "\n".join(warnings)))


def assert_warn_line(name, warnings):
    check("%s warnings point at source lines" % name,
          all(w.startswith("line ") for w in warnings),
          "  warnings: %s" % warnings)


# ---------------------------------------------------------------- Java ---

def test_java_bean():
    name = "java/bean->data-class"
    src = '''
public class Point {
  private final double x;
  private final double y;
  public Point(double x, double y) { this.x = x; this.y = y; }
  public double getX() { return x; }
  public double getY() { return y; }
  public boolean isOrigin() { return x == 0 && y == 0; }
}
'''
    out, w = convert(src, "java", "Point.java")
    assert_parses(name, out)
    assert_not_in(name, out, "fn getX")
    # isOrigin is a real method -> regular class with init, not a data class
    check("%s keeps real methods in a class" % name,
          "fn isOrigin" in out and "data class" not in out
          and "init(x: Float, y: Float)" in out)


def test_java_pure_bean():
    name = "java/pure-bean"
    src = '''
public class Pair {
  private String a;
  private String b;
  public String getA() { return a; }
  public String getB() { return b; }
}
'''
    out, w = convert(src, "java", "Pair.java")
    assert_parses(name, out)
    assert_in(name, out, "data class Pair(a: String, b: String)")


def test_java_class_methods():
    name = "java/class+methods+loops"
    src = '''
public class Counter {
  private int count;
  public Counter() { this.count = 0; }
  public void inc() { this.count = this.count + 1; }
  public int total(List<Integer> xs) {
    int sum = 0;
    for (int x : xs) {
      sum = sum + x;
    }
    int i = 0;
    while (i < 10) {
      sum = sum + i;
      i++;
    }
    for (int j = 0; j < 3; j++) {
      sum = sum + j;
    }
    if (sum > 100) {
      return 100;
    } else {
      return sum;
    }
  }
}
'''
    out, w = convert(src, "java", "Counter.java")
    assert_parses(name, out)
    assert_in(name, out, "class Counter {")
    assert_in(name, out, "init() {")
    assert_in(name, out, "fn inc() {")
    assert_in(name, out, "this.count = this.count + 1")
    assert_in(name, out, "var sum: Int = 0")
    assert_in(name, out, "for x in xs {")
    assert_in(name, out, "while i < 10 {")
    assert_in(name, out, "i = i + 1")
    assert_in(name, out, "while j < 3 {")   # classic for -> while
    assert_in(name, out, "if sum > 100 {")
    assert_in(name, out, "} else {")
    assert_warn(name, w, "C-style for")


def test_java_null_checks():
    name = "java/null-checks"
    src = '''
public class C {
  public String name(String s) {
    if (s != null) {
      return s;
    }
    return "none";
  }
  public String other(String s) {
    if (s == null) {
      return "none";
    }
    return s;
  }
}
'''
    out, w = convert(src, "java", "C.java")
    assert_parses(name, out)
    assert_warn(name, w, "null check on 's'")
    assert_warn(name, w, "Option")
    assert_in(name, out, "// match s {")
    assert_in(name, out, "//   Some(v) =>")
    assert_warn_line(name, w)


def test_java_try_catch():
    name = "java/try-catch"
    src = '''
public class C {
  public String load(String path) throws java.io.IOException {
    try {
      return read(path);
    } catch (Exception e) {
      return "err";
    }
  }
  public void boom() {
    throw new RuntimeException("x");
  }
}
'''
    out, w = convert(src, "java", "C.java")
    assert_parses(name, out)
    assert_in(name, out, "-> Result[String, String]")
    assert_warn(name, w, "try/catch")
    assert_warn(name, w, "flattened to Result")
    assert_in(name, out, "// catch (Exception e)")
    # throw outside a throws-method is dropped loudly, not silently
    assert_warn(name, w, "'throw' outside a 'throws' method")
    assert_in(name, out, "// UNCONVERTED (throw)")


def test_java_imports():
    name = "java/imports"
    src = '''
import java.util.List;
import java.util.ArrayList;
import com.example.Widget;
import org.acme.Gadget;

public class C {
  private List<String> xs;
}
'''
    out, w = convert(src, "java", "C.java")
    assert_parses(name, out)
    assert_not_in(name, out, "java.util")
    assert_in(name, out, "import widget")
    assert_in(name, out, "import gadget")
    assert_warn(name, w, "VERIFY the module mapping")


def test_java_unmappable():
    name = "java/unmappable-warns"
    src = '''
import java.util.Map;

public class C {
  private Map<String, Integer> m;
  public void f(Object o) {
    synchronized (o) {
      if (o instanceof String) {
        System.out.println("s");
      }
    }
    Runnable r = () -> System.out.println("x");
  }
  public Class<?> g() { return getClass(); }
}
'''
    out, w = convert(src, "java", "C.java")
    assert_parses(name, out)
    assert_warn(name, w, "Map has no Nova equivalent")
    assert_warn(name, w, "synchronized")
    assert_warn(name, w, "instanceof")
    assert_warn(name, w, "lambda")
    assert_warn_line(name, w)


def test_java_inheritance():
    name = "java/inheritance"
    src = '''
public class Circle extends Shape implements Named {
  private double radius;
  public Circle(double radius) { this.radius = radius; }
  @Override
  public double area() { return 3.14 * radius * radius; }
}
'''
    out, w = convert(src, "java", "Circle.java")
    assert_parses(name, out)
    assert_in(name, out, "class Circle extends Shape implements Named {")
    assert_in(name, out, "override fn area()")


def test_java_static():
    name = "java/static-hoisting"
    src = '''
public class Util {
  private static int calls = 0;
  public static int next() {
    calls = calls + 1;
    return calls;
  }
}
'''
    out, w = convert(src, "java", "Util.java")
    assert_parses(name, out)
    assert_in(name, out, "let calls: Int = 0")
    assert_in(name, out, "fn next() -> Int")
    assert_warn(name, w, "hoisted to module level")


def test_java_switch():
    name = "java/switch"
    src = '''
public class C {
  public String name(int n) {
    String s;
    switch (n) {
      case 1:
        s = "one";
        break;
      case 2:
      case 3:
        s = "few";
        break;
      default:
        s = "many";
    }
    return s;
  }
}
'''
    out, w = convert(src, "java", "C.java")
    assert_parses(name, out)
    assert_in(name, out, "if n == 1 {")
    assert_in(name, out, "} else if n == 2 or n == 3 {")
    assert_in(name, out, "} else {")
    assert_warn(name, w, "switch")


def test_java_exprs():
    name = "java/expressions"
    src = '''
public class C {
  public String f(String a, String b, int n) {
    boolean ok = a.equals(b) && n > 0;
    String t = ok ? a : b;
    int len = a.length();
    return t + len;
  }
}
'''
    out, w = convert(src, "java", "C.java")
    assert_parses(name, out)
    assert_in(name, out, "(a == b) and n > 0")
    assert_in(name, out, "if ok { a } else { b }")
    assert_in(name, out, "len(a)")


# ----------------------------------------------------------------- JSX ---

def test_java_accessor_calls():
    name = "java/accessor-calls"
    src = '''
public class Point {
  private double x;
  public double getX() { return x; }
  public void setX(double v) { this.x = v; }
  public double copy(Point o) {
    this.setX(o.getX());
    return this.getX();
  }
}
'''
    out, w = convert(src, "java", "Point.java")
    assert_parses(name, out)
    assert_in(name, out, "this.x = o.x")
    assert_in(name, out, "return this.x")
    assert_warn(name, w, "folded into field")


def test_jsx_counter():
    name = "jsx/counter"
    src = '''
import React, { useState } from 'react';

export default function Counter({ title }) {
  const [count, setCount] = useState(0);
  return (
    <div className="counter">
      <h1>{title}</h1>
      <p>{count}</p>
      <button onClick={() => setCount(count + 1)}>Increment</button>
    </div>
  );
}
'''
    out, w = convert(src, "jsx", "Counter.jsx")
    assert_parses(name, out)
    assert_in(name, out, "component Counter(title: Any) {")
    assert_in(name, out, "state count: Int = 0")
    assert_in(name, out, "onClick={count = count + 1}")
    assert_in(name, out, '<div class="counter">')
    assert_in(name, out, "view {")
    assert_warn(name, w, "untyped (JS)")


def test_jsx_list_map():
    name = "jsx/map->for"
    src = '''
export function List({ items }) {
  return (
    <ul>
      {items.map(item => <li key={item.id}>{item.name}</li>)}
    </ul>
  );
}
'''
    out, w = convert(src, "jsx", "List.jsx")
    assert_parses(name, out)
    assert_in(name, out, "<for item in items>")
    assert_in(name, out, "</for>")
    assert_in(name, out, "{item.name}")
    assert_not_in(name, out, "key={")
    assert_warn(name, w, "key")


def test_jsx_conditionals():
    name = "jsx/conditionals"
    src = '''
export function Card({ show, ok }) {
  return (
    <div>
      {show && <p>visible</p>}
      {ok ? <b>yes</b> : <i>no</i>}
    </div>
  );
}
'''
    out, w = convert(src, "jsx", "Card.jsx")
    assert_parses(name, out)
    assert_in(name, out, "<if show>")
    assert_in(name, out, "<if ok>")
    assert_in(name, out, "// (ternary else-branch)")
    assert_warn(name, w, "ternary")


def test_jsx_props_object():
    name = "jsx/props-object"
    src = '''
export function Hello(props) {
  return <h1>{props.name}</h1>;
}
'''
    out, w = convert(src, "jsx", "Hello.jsx")
    assert_parses(name, out)
    assert_in(name, out, "component Hello(name: Any)")
    assert_in(name, out, "{name}")
    assert_warn(name, w, "destructured into params")


def test_jsx_hooks_warn():
    name = "jsx/hooks-warn"
    src = '''
import { useState, useEffect, useRef, useMemo } from 'react';
export function W() {
  const [x, setX] = useState(1);
  const r = useRef(null);
  const m = useMemo(() => x * 2, [x]);
  useEffect(() => { setX(2); }, []);
  return <p>{x}</p>;
}
'''
    out, w = convert(src, "jsx", "W.jsx")
    assert_parses(name, out)
    assert_warn(name, w, "useEffect")
    assert_warn(name, w, "useRef")
    assert_warn(name, w, "useMemo")
    assert_in(name, out, "// UNCONVERTED (useEffect)")
    assert_warn_line(name, w)


def test_jsx_imports():
    name = "jsx/imports"
    src = '''
import React from 'react';
import './app.css';
import Header from './Header';
import axios from 'axios';
export function A() { return <p>hi</p>; }
'''
    out, w = convert(src, "jsx", "A.jsx")
    assert_parses(name, out)
    assert_not_in(name, out, "import react")
    assert_in(name, out, "import header")
    assert_warn(name, w, "stylesheet import")
    assert_warn(name, w, "external package 'axios'")


def test_jsx_handlers():
    name = "jsx/handlers"
    src = '''
import { useState } from 'react';
export function B({ name }) {
  const [count, setCount] = useState(0);
  const bump = () => setCount(c => c + 1);
  const label = `hi ${name}`;
  return (
    <div>
      <button onClick={bump}>Bump</button>
      <button onClick={() => setCount(0)}>Reset</button>
      <p>{label}</p>
    </div>
  );
}
'''
    out, w = convert(src, "jsx", "B.jsx")
    assert_parses(name, out)
    # updater arrow inlined through the handler template
    assert_in(name, out, "onClick={count = count + 1}")
    assert_in(name, out, "onClick={count = 0}")
    # template literal -> string concat, local inlined
    assert_in(name, out, '"hi " + str(name)')


def test_jsx_warnings_drops():
    name = "jsx/drops-warn"
    src = '''
import { useState } from 'react';
export function D(props) {
  const [x, setX] = useState(0);
  return (
    <div style={{color: "red"}} {...props} dangerouslySetInnerHTML={{__html: x}}>
      <p>{x}</p>
    </div>
  );
}
'''
    out, w = convert(src, "jsx", "D.jsx")
    assert_parses(name, out)
    assert_not_in(name, out, "<div style")
    assert_not_in(name, out, "<div dangerously")
    assert_warn(name, w, "CSS-in-JS")
    assert_warn(name, w, "spread attributes")
    assert_warn(name, w, "dangerouslySetInnerHTML")


def test_jsx_map_paren():
    name = "jsx/map-paren-callback"
    src = '''
export function L({ items }) {
  return (
    <ul>
      {items.map(item => (
        <li>{item}</li>
      ))}
    </ul>
  );
}
'''
    out, w = convert(src, "jsx", "L.jsx")
    assert_parses(name, out)
    assert_in(name, out, "<for item in items>")


def test_jsx_template_call():
    name = "jsx/template-call-inline"
    src = '''
import { useState } from 'react';
export function S({ products }) {
  const [cart, setCart] = useState([]);
  const addToCart = (p) => setCart([...cart, p]);
  return (
    <div>
      {products.map(p => <button key={p.id} onClick={() => addToCart(p)}>add</button>)}
    </div>
  );
}
'''
    out, w = convert(src, "jsx", "S.jsx")
    assert_parses(name, out)
    # template inlined + [...cart, p] append pattern -> push
    assert_in(name, out, "onClick={cart.push(p)}")
    # not double-processed as a module-level fn
    assert_not_in(name, out, "fn addToCart")


def test_jsx_ternary_expr():
    name = "jsx/ternary-expr"
    src = '''
export function T({ x }) {
  return <p>{x > 0 ? "pos" : "neg"}</p>;
}
'''
    out, w = convert(src, "jsx", "T.jsx")
    assert_parses(name, out)
    assert_in(name, out, '{if x > 0 { "pos" } else { "neg" }}')


def test_jsx_expr_bodied_arrow():
    name = "jsx/expr-bodied-arrow"
    src = '''
import React from 'react';
const Hi = ({ t }) => <h1>{t}</h1>;
export default Hi;
'''
    out, w = convert(src, "jsx", "Hi.jsx")
    assert_parses(name, out)
    assert_in(name, out, "component Hi(t: Any) {")
    assert_in(name, out, "{t}")


def test_bad_flavor():
    try:
        migrate("x", "x.java", "rust")
        check("migrate rejects unknown flavor", False,
              "  no ValueError raised")
    except ValueError:
        check("migrate rejects unknown flavor", True)


def test_warnings_summary_in_output():
    out, w = convert("public class A {\n private int x;\n}\n", "java",
                     "A.java")
    assert_parses("summary", out)
    check("warnings summary block is in the output",
          "// ---- migrate warnings:" in out)


def main():
    print("== migrate: java ==")
    test_java_bean()
    test_java_pure_bean()
    test_java_class_methods()
    test_java_null_checks()
    test_java_try_catch()
    test_java_imports()
    test_java_unmappable()
    test_java_inheritance()
    test_java_static()
    test_java_switch()
    test_java_exprs()
    print("== migrate: jsx ==")
    test_jsx_counter()
    test_jsx_list_map()
    test_jsx_conditionals()
    test_jsx_props_object()
    test_jsx_hooks_warn()
    test_jsx_imports()
    test_jsx_handlers()
    test_jsx_warnings_drops()
    print("== migrate: misc ==")
    test_bad_flavor()
    test_warnings_summary_in_output()
    print()
    if FAILURES:
        print("%d FAILURE(S): %s" % (len(FAILURES),
                                     ", ".join(FAILURES)))
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
