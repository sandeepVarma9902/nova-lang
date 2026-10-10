# types.py — Nova's type representation.
#
# Nova is statically typed with inference. There are no primitive/object
# dualities and no implicit conversions: every value has exactly one type.

class Type:
    pass


class PrimType(Type):
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, PrimType) and other.name == self.name

    def __hash__(self):
        return hash(("prim", self.name))

    def __repr__(self):
        return self.name


INT = PrimType("Int")
FLOAT = PrimType("Float")
STRING = PrimType("String")
BOOL = PrimType("Bool")
UNIT = PrimType("Unit")


class ListType(Type):
    def __init__(self, elem):
        self.elem = elem

    def __eq__(self, other):
        return isinstance(other, ListType) and other.elem == self.elem

    def __hash__(self):
        return hash(("list", self.elem))

    def __repr__(self):
        return "List[%s]" % self.elem


class OptionType(Type):
    def __init__(self, elem):
        self.elem = elem

    def __eq__(self, other):
        return isinstance(other, OptionType) and other.elem == self.elem

    def __hash__(self):
        return hash(("option", self.elem))

    def __repr__(self):
        return "Option[%s]" % self.elem


class ResultType(Type):
    def __init__(self, ok, err):
        self.ok = ok
        self.err = err

    def __eq__(self, other):
        return (
            isinstance(other, ResultType)
            and other.ok == self.ok
            and other.err == self.err
        )

    def __hash__(self):
        return hash(("result", self.ok, self.err))

    def __repr__(self):
        return "Result[%s, %s]" % (self.ok, self.err)


class ClassType(Type):
    """Nominal class type."""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, ClassType) and other.name == self.name

    def __hash__(self):
        return hash(("class", self.name))

    def __repr__(self):
        return self.name


class InterfaceType(Type):
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, InterfaceType) and other.name == self.name

    def __hash__(self):
        return hash(("iface", self.name))

    def __repr__(self):
        return self.name


class ComponentType(Type):
    """The component itself (e.g. Counter). Calling it builds an instance."""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, ComponentType) and other.name == self.name

    def __hash__(self):
        return hash(("component", self.name))

    def __repr__(self):
        return self.name


class InstanceType(Type):
    """A mounted/unmounted instance of a component."""

    def __init__(self, comp):
        self.comp = comp

    def __eq__(self, other):
        return isinstance(other, InstanceType) and other.comp == self.comp

    def __hash__(self):
        return hash(("instance", self.comp))

    def __repr__(self):
        return "Instance[%s]" % self.comp


class ModuleType(Type):
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, ModuleType) and other.name == self.name

    def __hash__(self):
        return hash(("module", self.name))

    def __repr__(self):
        return "module %s" % self.name


class TaskType(Type):
    """A spawned concurrent task. task.await() : T."""

    def __init__(self, elem):
        self.elem = elem

    def __eq__(self, other):
        return isinstance(other, TaskType) and other.elem == self.elem

    def __hash__(self):
        return hash(("task", self.elem))

    def __repr__(self):
        return "Task[%s]" % self.elem


class StreamType(Type):
    """A named pub/sub topic handle. Declared with `stream name: T`."""

    def __init__(self, elem):
        self.elem = elem

    def __eq__(self, other):
        return isinstance(other, StreamType) and other.elem == self.elem

    def __hash__(self):
        return hash(("stream", self.elem))

    def __repr__(self):
        return "Stream[%s]" % self.elem


class SubscriptionType(Type):
    """A consumer-group subscription on a stream."""

    def __init__(self, elem):
        self.elem = elem

    def __eq__(self, other):
        return (isinstance(other, SubscriptionType)
                and other.elem == self.elem)

    def __hash__(self):
        return hash(("subscription", self.elem))

    def __repr__(self):
        return "Subscription[%s]" % self.elem


class MessageType(Type):
    """One consumed record: topic, offset, key, value."""

    def __init__(self, elem):
        self.elem = elem

    def __eq__(self, other):
        return (isinstance(other, MessageType)
                and other.elem == self.elem)

    def __hash__(self):
        return hash(("message", self.elem))

    def __repr__(self):
        return "Message[%s]" % self.elem


class AnyType(Type):
    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash("any")

    def __repr__(self):
        return "Any"


ANY = AnyType()


def is_subtype(sub, sup, classes):
    """Nominal subtyping for classes/interfaces; structural for generics."""
    if sub == sup or sup == ANY or sub == ANY:
        return True
    if isinstance(sub, ClassType) and isinstance(sup, ClassType):
        info = classes.get(sub.name)
        while info is not None and info.parent:
            if info.parent == sup.name:
                return True
            info = classes.get(info.parent)
        return False
    if isinstance(sub, ClassType) and isinstance(sup, InterfaceType):
        return sup.name in all_interfaces(sub.name, classes)
    return False


def all_interfaces(class_name, classes):
    out = set()
    info = classes.get(class_name)
    while info is not None:
        out.update(info.interfaces)
        info = classes.get(info.parent) if info.parent else None
    return out


class ClassInfo:
    def __init__(self, name, parent, interfaces, is_data=False):
        self.name = name
        self.parent = parent          # str | None
        self.interfaces = interfaces  # [str]
        self.is_data = is_data


class JsonType(Type):
    """Parsed JSON document: object | array | string | number | bool | null."""

    def __eq__(self, other):
        return isinstance(other, JsonType)

    def __hash__(self):
        return hash("json")

    def __repr__(self):
        return "Json"


JSON = JsonType()


class VectorType(Type):
    """Fixed-dimension embedding vector. Vector[1536]."""

    def __init__(self, dim):
        self.dim = dim

    def __eq__(self, other):
        return isinstance(other, VectorType) and other.dim == self.dim

    def __hash__(self):
        return hash(("vector", self.dim))

    def __repr__(self):
        return "Vector[%d]" % self.dim


class RequestType(Type):
    """An inbound HTTP request (Nova Boot)."""

    def __eq__(self, other):
        return isinstance(other, RequestType)

    def __hash__(self):
        return hash("request")

    def __repr__(self):
        return "Request"


REQUEST = RequestType()


class ResponseType(Type):
    """An outbound HTTP response (Nova Boot)."""

    def __eq__(self, other):
        return isinstance(other, ResponseType)

    def __hash__(self):
        return hash("response")

    def __repr__(self):
        return "Response"


RESPONSE = ResponseType()


class RouterType(Type):
    """A Nova Boot router: routes + middleware + static dirs."""

    def __eq__(self, other):
        return isinstance(other, RouterType)

    def __hash__(self):
        return hash("router")

    def __repr__(self):
        return "Router"


ROUTER = RouterType()


class DbType(Type):
    """An open database connection handle."""

    def __eq__(self, other):
        return isinstance(other, DbType)

    def __hash__(self):
        return hash("db")

    def __repr__(self):
        return "Db"


DB = DbType()


class QueryType(Type):
    """A fluent query builder over one table."""

    def __init__(self, table):
        self.table = table  # str | None (None = table not statically known)

    def __eq__(self, other):
        return isinstance(other, QueryType) and other.table == self.table

    def __hash__(self):
        return hash(("query", self.table))

    def __repr__(self):
        return "Query[%s]" % (self.table or "?")
