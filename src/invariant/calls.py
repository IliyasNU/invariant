"""Resolve call targets to qualified names, statically and conservatively.

``import os as operating; operating.system()`` calls ``os.system``,
``eval()`` calls ``builtins.eval`` and ``@functools.cache`` calls
``functools.cache``. A name is followed only when every binding it has in the
scope Python would look it up in is an import of the same target, or when
nothing binds it and it is a builtin. Anything else (a parameter, an
assignment, a fallback ``except ImportError`` branch, a star import that could
rebind it) is not proof of what is called, so the call stays unresolved.
"""

import ast
import builtins
from collections.abc import Iterator
from dataclasses import dataclass, field

from invariant.paths import absolute_module

BUILTINS = frozenset(dir(builtins))

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# Not an import: whatever this name holds is unknown statically.
_OTHER = None


@dataclass
class Scope:
    # "module", "function", "class", "comprehension", or "annotation" for the
    # type parameters of ``def f[T]()``, ``class A[T]`` and ``type X[T] = ...``
    kind: str
    parent: Scope | None
    # name -> import targets, with _OTHER for any binding that is not an import
    bindings: dict[str, list[str | None]] = field(default_factory=dict)
    global_names: set[str] = field(default_factory=set)
    nonlocal_names: set[str] = field(default_factory=set)
    star_import: bool = False

    def bind(self, name: str, target: str | None = _OTHER) -> None:
        self.bindings.setdefault(name, []).append(target)


@dataclass(frozen=True)
class ResolvedCall:
    # The call, or a decorator applied without parentheses.
    node: ast.expr
    # The qualified name of what is called, e.g. "os.system".
    target: str


class _Collector:
    """Build the scopes of a module and remember which scope each call is in."""

    def __init__(self, package: tuple[str, ...]) -> None:
        self.package = package
        self.calls: list[tuple[ast.expr, Scope]] = []
        self.pending: list[tuple[ast.AST, Scope]] = []

    def module(self, tree: ast.Module) -> None:
        scope = Scope("module", None)
        self.module_scope = scope
        self.visit_all(tree.body, scope)
        # A work list rather than recursion: ``1 + 1 + ... + 1`` nests as deep
        # as it is long. Bindings are only looked up once all are collected,
        # so the order nodes are processed in does not matter.
        while self.pending:
            self.process(*self.pending.pop())

    # Binding targets -------------------------------------------------------

    def bind_target(self, target: ast.AST, scope: Scope) -> None:
        if isinstance(target, ast.Name):
            self.bind(target.id, scope)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.bind_target(item, scope)
        elif isinstance(target, ast.Starred):
            self.bind_target(target.value, scope)
        else:
            # Attribute and subscript targets bind nothing, but evaluate parts.
            self.visit(target, scope)

    def bind(self, name: str, scope: Scope, target: str | None = _OTHER) -> None:
        owner = self.owner(name, scope)
        owner.bind(name, target)

    def owner(self, name: str, scope: Scope) -> Scope:
        """The scope a binding of ``name`` made in ``scope`` lands in."""
        if name in scope.global_names:
            return self.module_scope
        if name in scope.nonlocal_names:
            # The enclosing function's own bindings may not be collected yet;
            # the nearest enclosing function is where nonlocal names live.
            outer = scope.parent
            while outer is not None and outer.kind != "function":
                outer = outer.parent
            return outer or self.module_scope
        return scope

    # Visiting --------------------------------------------------------------

    def visit_all(self, nodes: list[ast.AST], scope: Scope) -> None:
        for node in nodes:
            self.visit(node, scope)

    def visit(self, node: ast.AST, scope: Scope) -> None:
        self.pending.append((node, scope))

    def process(self, node: ast.AST, scope: Scope) -> None:
        if isinstance(node, _FUNCTIONS):
            self.function(node, scope)
        elif isinstance(node, ast.ClassDef):
            self.decorators(node, scope)
            types = self.type_parameters(node.type_params, scope)
            self.visit_all(node.bases, types)
            self.visit_all([item.value for item in node.keywords], types)
            body = Scope("class", types)
            self.declare(node.body, body)
            self.visit_all(node.body, body)
            self.bind(node.name, scope)
        elif isinstance(node, _COMPREHENSIONS):
            self.comprehension(node, scope)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    self.bind(alias.asname, scope, alias.name)
                else:
                    # ``import a.b`` binds ``a`` to the package a.
                    top = alias.name.split(".")[0]
                    self.bind(top, scope, top)
        elif isinstance(node, ast.ImportFrom):
            module = absolute_module(self.package, node.level, node.module)
            for alias in node.names:
                if alias.name == "*":
                    scope.star_import = True
                elif module is None:
                    self.bind(alias.asname or alias.name, scope)
                else:
                    self.bind(
                        alias.asname or alias.name, scope, f"{module}.{alias.name}"
                    )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            pass  # declared up front by declare()
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            if node.value is not None:
                self.visit(node.value, scope)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                self.bind_target(target, scope)
            if isinstance(node, ast.AnnAssign):
                self.visit(node.annotation, scope)
        elif isinstance(node, ast.NamedExpr):
            self.visit(node.value, scope)
            # A walrus in a comprehension binds in the enclosing function.
            while scope.kind == "comprehension" and scope.parent is not None:
                scope = scope.parent
            self.bind_target(node.target, scope)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self.visit(node.iter, scope)
            self.bind_target(node.target, scope)
            self.visit_all(node.body + node.orelse, scope)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self.visit(item.context_expr, scope)
                if item.optional_vars is not None:
                    self.bind_target(item.optional_vars, scope)
            self.visit_all(node.body, scope)
        elif isinstance(node, ast.ExceptHandler):
            if node.type is not None:
                self.visit(node.type, scope)
            if node.name:
                self.bind(node.name, scope)
            self.visit_all(node.body, scope)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                self.bind_target(target, scope)
        elif isinstance(node, ast.TypeAlias):
            self.bind_target(node.name, scope)
            self.visit(node.value, self.type_parameters(node.type_params, scope))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name:
                self.bind(node.name, scope)
            for child in ast.iter_child_nodes(node):
                self.visit(child, scope)
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                self.bind(node.rest, scope)
            for child in ast.iter_child_nodes(node):
                self.visit(child, scope)
        else:
            if isinstance(node, ast.Call):
                self.calls.append((node, scope))
            for child in ast.iter_child_nodes(node):
                self.visit(child, scope)

    def function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, scope: Scope
    ) -> None:
        # Decorators and defaults run in the enclosing scope, annotations also
        # see the type parameters.
        arguments = node.args
        types = scope
        if not isinstance(node, ast.Lambda):
            self.decorators(node, scope)
            types = self.type_parameters(node.type_params, scope)
            if node.returns is not None:
                self.visit(node.returns, types)
        self.visit_all(arguments.defaults, scope)
        self.visit_all(
            [item for item in arguments.kw_defaults if item is not None], scope
        )
        parameters = [
            *arguments.posonlyargs,
            *arguments.args,
            *([arguments.vararg] if arguments.vararg else []),
            *arguments.kwonlyargs,
            *([arguments.kwarg] if arguments.kwarg else []),
        ]
        for parameter in parameters:
            if parameter.annotation is not None:
                self.visit(parameter.annotation, types)
        body = Scope("function", types)
        for parameter in parameters:
            body.bind(parameter.arg)
        statements = node.body if isinstance(node.body, list) else [node.body]
        self.declare(statements, body)
        self.visit_all(statements, body)
        if not isinstance(node, ast.Lambda):
            self.bind(node.name, scope)

    def decorators(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, scope: Scope
    ) -> None:
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                # ``@cache`` calls ``cache`` with what it decorates.
                self.calls.append((decorator, scope))
            self.visit(decorator, scope)

    def type_parameters(self, parameters: list[ast.type_param], scope: Scope) -> Scope:
        """The scope ``def f[T]()`` binds ``T`` in, between ``f`` and its body."""
        if not parameters:
            return scope
        inner = Scope("annotation", scope)
        for parameter in parameters:
            inner.bind(parameter.name)
        for parameter in parameters:
            # A bound or default (``T: Base = Default``) may use other parameters.
            self.visit_all(list(ast.iter_child_nodes(parameter)), inner)
        return inner

    def comprehension(self, node: ast.expr, scope: Scope) -> None:
        generators = node.generators
        # The first iterable is evaluated in the enclosing scope.
        self.visit(generators[0].iter, scope)
        inner = Scope("comprehension", scope)
        for index, generator in enumerate(generators):
            if index:
                self.visit(generator.iter, inner)
            self.bind_target(generator.target, inner)
            self.visit_all(generator.ifs, inner)
        parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        self.visit_all(parts, inner)

    def declare(self, statements: list[ast.stmt], scope: Scope) -> None:
        """Record ``global``/``nonlocal`` before bindings, as Python does."""
        for node in _own_nodes(statements):
            if isinstance(node, ast.Global):
                scope.global_names.update(node.names)
            elif isinstance(node, ast.Nonlocal):
                scope.nonlocal_names.update(node.names)


def _own_nodes(statements: list[ast.stmt]) -> Iterator[ast.AST]:
    """Nodes of a scope's body, not descending into nested scopes."""
    stack: list[ast.AST] = list(statements)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (*_FUNCTIONS, ast.ClassDef, *_COMPREHENSIONS)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _lookup(name: str, scope: Scope, module: Scope) -> str | None:
    """The single import target ``name`` refers to in ``scope``, if provable."""
    if name in scope.global_names:
        scope = module
    current: Scope | None = scope
    first = True
    while current is not None:
        # ``from x import *`` may rebind any name, even one imported explicitly.
        if current.star_import:
            return None
        # Class bodies are not visible from the scopes nested inside them.
        if (first or current.kind != "class") and name in current.bindings:
            targets = set(current.bindings[name])
            if len(targets) != 1:
                return None  # bound to different things
            return targets.pop()  # None (_OTHER) unless the binding is an import
        first = False
        current = current.parent
    return f"builtins.{name}" if name in BUILTINS else None


def resolve_calls(tree: ast.Module, package: tuple[str, ...]) -> list[ResolvedCall]:
    """Every call in ``tree`` whose target can be proven, in source order."""
    collector = _Collector(package)
    collector.module(tree)
    resolved: list[ResolvedCall] = []
    for node, scope in collector.calls:
        attributes: list[str] = []
        callee = node.func if isinstance(node, ast.Call) else node
        while isinstance(callee, ast.Attribute):
            attributes.append(callee.attr)
            callee = callee.value
        if not isinstance(callee, ast.Name):
            continue
        base = _lookup(callee.id, scope, collector.module_scope)
        if base is None:
            continue
        resolved.append(ResolvedCall(node, ".".join([base, *reversed(attributes)])))
    return sorted(resolved, key=lambda call: (call.node.lineno, call.node.col_offset))
