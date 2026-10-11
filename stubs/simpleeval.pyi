# The part of simpleeval this project uses (the package ships no type hints).

import ast
from collections.abc import Callable, Mapping

class InvalidExpression(Exception): ...  # noqa: N818

class NameNotDefined(InvalidExpression):
    def __init__(self, name: str, expression: str) -> None: ...

class SimpleEval:
    def __init__(
        self,
        operators: Mapping[type[ast.AST], Callable[..., object]] | None = None,
        functions: Mapping[str, Callable[..., object]] | None = None,
        names: Mapping[str, object] | Callable[[ast.Name], object] | None = None,
    ) -> None: ...
    @staticmethod
    def parse(expr: str) -> ast.stmt: ...
    def eval(self, expr: str, previously_parsed: ast.stmt | None = None) -> object: ...

class EvalWithCompoundTypes(SimpleEval): ...
