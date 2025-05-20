from __future__ import annotations

import abc
from typing import Optional, List


class SyntheticMethod:
    class MethodIdentifier(abc.ABC):
        @abc.abstractmethod
        def make_call_expr(self, this_reference: str, args: List[str]) -> str:
            return ""

        @abc.abstractmethod
        def get_name(self) -> str:
            return ""

    class Named(MethodIdentifier):
        def __init__(self, name: str):
            self._name = name

        def make_call_expr(self, this_reference: str, args: List[str]) -> str:
            return f"{this_reference}.{self._name}({', '.join(args)})"

        def get_name(self) -> str:
            return self._name

    class SubscriptOperator(MethodIdentifier):
        def make_call_expr(self, this_reference: str, args: List[str]) -> str:
            return f"{this_reference}[{', '.join(args)}]"

        def get_name(self) -> str:
            return "operator[]"

    class Call:
        def __init__(self, identifier: SyntheticMethod.MethodIdentifier, args: List[str]):
            self._identifier = identifier
            self._args = args

        def make_call_expr(self, this_reference: str) -> str:
            return self._identifier.make_call_expr(this_reference, self._args)

    @staticmethod
    def named_method(name: str) -> SyntheticMethod:
        return SyntheticMethod(SyntheticMethod.Named(name))

    @staticmethod
    def subscript_operator() -> SyntheticMethod:
        return SyntheticMethod(SyntheticMethod.SubscriptOperator())

    def __init__(self, identifier: SyntheticMethod.MethodIdentifier):
        self._identifier = identifier

    @property
    def identifier(self) -> MethodIdentifier:
        return self._identifier

    @property
    def name(self) -> str:
        return self._identifier.get_name()

    def method_call(self, args: Optional[List[str]] = None) -> Call:
        return SyntheticMethod.Call(self._identifier, args or [])


class SyntheticMethodDefinition:
    def __init__(self, full_name: str, body_substitution: str, name_uses_regex: bool):
        self.full_name = full_name
        self.body_substitution = body_substitution
        self.name_uses_regex = name_uses_regex
