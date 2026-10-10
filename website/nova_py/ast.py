# ast.py — Nova abstract syntax tree nodes.
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Node:
    line: int = 0
    col: int = 0


# ---------------------------------------------------------------- types ----
@dataclass
class TypeNode(Node):
    name: str = ""
    args: List["TypeNode"] = field(default_factory=list)


@dataclass
class ConstTypeArg(Node):
    """A constant type argument, e.g. the 1536 in Vector[1536]."""
    value: int = 0


# -------------------------------------------------------------- program ----
@dataclass
class Program(Node):
    stmts: List[Node] = field(default_factory=list)


@dataclass
class Import(Node):
    name: str = ""


# ---------------------------------------------------------- declarations ----
@dataclass
class Param(Node):
    name: str = ""
    type: Optional[TypeNode] = None


@dataclass
class LetDecl(Node):
    name: str = ""
    ann: Optional[TypeNode] = None
    expr: Optional[Node] = None


@dataclass
class VarDecl(Node):
    name: str = ""
    ann: Optional[TypeNode] = None
    expr: Optional[Node] = None


@dataclass
class FnDecl(Node):
    name: str = ""
    params: List[Param] = field(default_factory=list)
    ret: Optional[TypeNode] = None
    requires: List[Node] = field(default_factory=list)
    ensures: List[Node] = field(default_factory=list)
    body: Optional[Node] = None
    effect: str = "pure"  # pure | effect | io | net | async | async io | async net


@dataclass
class StreamDecl(Node):
    """A named pub/sub topic: `stream events: String [= "topic-name"]`."""
    name: str = ""
    type: Optional[TypeNode] = None
    topic: Optional[str] = None  # defaults to name


@dataclass
class AiDecl(Node):
    """First-class AI function: the return TYPE is the output schema."""
    name: str = ""
    params: List[Param] = field(default_factory=list)
    ret: Optional[TypeNode] = None
    config: dict = field(default_factory=dict)  # model, temperature, ...
    body: Optional[Node] = None


@dataclass
class FieldDecl(Node):
    name: str = ""
    type: Optional[TypeNode] = None
    mutable: bool = False


@dataclass
class InitDecl(Node):
    params: List[Param] = field(default_factory=list)
    body: Optional[Node] = None


@dataclass
class MethodDecl(Node):
    name: str = ""
    params: List[Param] = field(default_factory=list)
    ret: Optional[TypeNode] = None
    requires: List[Node] = field(default_factory=list)
    ensures: List[Node] = field(default_factory=list)
    body: Optional[Node] = None
    effect: str = "pure"  # "pure" | "effect" | "io" | "net"
    is_override: bool = False


@dataclass
class ClassDecl(Node):
    name: str = ""
    parent: Optional[str] = None
    interfaces: List[str] = field(default_factory=list)
    members: List[Node] = field(default_factory=list)
    is_data: bool = False


@dataclass
class InterfaceDecl(Node):
    name: str = ""
    methods: List[MethodDecl] = field(default_factory=list)


@dataclass
class StateDecl(Node):
    name: str = ""
    type: Optional[TypeNode] = None
    init: Optional[Node] = None


@dataclass
class ComponentDecl(Node):
    name: str = ""
    params: List[Param] = field(default_factory=list)
    states: List[StateDecl] = field(default_factory=list)
    view: List[Node] = field(default_factory=list)


# ------------------------------------------------------------ statements ----
@dataclass
class Block(Node):
    stmts: List[Node] = field(default_factory=list)


@dataclass
class ExprStmt(Node):
    expr: Optional[Node] = None


@dataclass
class Return(Node):
    expr: Optional[Node] = None


@dataclass
class IfStmt(Node):
    cond: Optional[Node] = None
    then_b: Optional[Block] = None
    else_b: Optional[Node] = None  # Block | IfStmt


@dataclass
class WhileStmt(Node):
    cond: Optional[Node] = None
    body: Optional[Block] = None


@dataclass
class ForStmt(Node):
    var: str = ""
    iter: Optional[Node] = None
    body: Optional[Block] = None


@dataclass
class SuperCall(Node):
    args: List[Tuple[Optional[str], Node]] = field(default_factory=list)


# ----------------------------------------------------------- expressions ----
@dataclass
class IntLit(Node):
    value: int = 0


@dataclass
class FloatLit(Node):
    value: float = 0.0


@dataclass
class StrLit(Node):
    value: str = ""


@dataclass
class BoolLit(Node):
    value: bool = False


@dataclass
class NoneLit(Node):
    pass


@dataclass
class Var(Node):
    name: str = ""


@dataclass
class This(Node):
    pass


@dataclass
class Super(Node):
    pass


@dataclass
class ResultVar(Node):
    """The `result` keyword inside ensures clauses."""
    pass


@dataclass
class SomeExpr(Node):
    expr: Optional[Node] = None


@dataclass
class OkExpr(Node):
    expr: Optional[Node] = None


@dataclass
class ErrExpr(Node):
    expr: Optional[Node] = None


@dataclass
class ListLit(Node):
    elems: List[Node] = field(default_factory=list)


@dataclass
class Call(Node):
    func: Optional[Node] = None
    args: List[Tuple[Optional[str], Node]] = field(default_factory=list)


@dataclass
class MethodCall(Node):
    obj: Optional[Node] = None
    name: str = ""
    args: List[Tuple[Optional[str], Node]] = field(default_factory=list)


@dataclass
class Field(Node):
    obj: Optional[Node] = None
    name: str = ""


@dataclass
class Index(Node):
    obj: Optional[Node] = None
    index: Optional[Node] = None


@dataclass
class Binary(Node):
    op: str = ""
    left: Optional[Node] = None
    right: Optional[Node] = None


@dataclass
class Unary(Node):
    op: str = ""
    expr: Optional[Node] = None


@dataclass
class Assign(Node):
    target: Optional[Node] = None
    value: Optional[Node] = None


@dataclass
class Question(Node):
    """Postfix `?` — unwrap Result/Option or propagate."""
    expr: Optional[Node] = None


@dataclass
class Spawn(Node):
    """spawn { block } — run the block on a worker thread, get Task[T]."""
    block: Optional[Block] = None


@dataclass
class SpawnAsync(Node):
    """spawn_async { block } — run the block as a cooperative tasklet on
    the event loop, get Task[T]. Never parallel; shares the loop thread."""
    block: Optional[Block] = None


@dataclass
class Nursery(Node):
    """nursery { block } — structured scope: every task spawned inside is
    joined before the block exits; on error, tasks are cancelled."""
    block: Optional[Block] = None


@dataclass
class ContextDecl(Node):
    """context name: Type = default — typed ambient value (no prop drilling)."""
    name: str = ""
    type: Optional[TypeNode] = None
    default: Optional[Node] = None


@dataclass
class Provide(Node):
    """provide name = value { block } — block-scoped context override."""
    name: str = ""
    value: Optional[Node] = None
    block: Optional[Block] = None


@dataclass
class Consume(Node):
    """consume name — read the nearest provided (or default) context value."""
    name: str = ""


@dataclass
class IfExpr(Node):
    cond: Optional[Node] = None
    then_b: Optional[Block] = None
    else_b: Optional[Block] = None


# --------------------------------------------------------------- patterns ----
@dataclass
class PatSome(Node):
    name: str = ""


@dataclass
class PatNone(Node):
    pass


@dataclass
class PatOk(Node):
    name: str = ""


@dataclass
class PatErr(Node):
    name: str = ""


@dataclass
class PatLit(Node):
    value: object = None


@dataclass
class PatWild(Node):
    pass


@dataclass
class PatVar(Node):
    name: str = ""


@dataclass
class MatchArm(Node):
    pattern: Optional[Node] = None
    expr: Optional[Node] = None


@dataclass
class Match(Node):
    scrutinee: Optional[Node] = None
    arms: List[MatchArm] = field(default_factory=list)


# ------------------------------------------------------------------- JSX ----
@dataclass
class JAttr(Node):
    name: str = ""
    value: Optional[Node] = None  # StrLit | expr | None (= boolean true)


@dataclass
class JElement(Node):
    tag: str = ""
    attrs: List[JAttr] = field(default_factory=list)
    children: List[Node] = field(default_factory=list)
    is_component: bool = False


@dataclass
class JText(Node):
    text: str = ""


@dataclass
class JExpr(Node):
    expr: Optional[Node] = None


@dataclass
class JFor(Node):
    var: str = ""
    iter: Optional[Node] = None
    children: List[Node] = field(default_factory=list)
