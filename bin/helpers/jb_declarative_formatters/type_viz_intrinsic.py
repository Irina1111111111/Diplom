from __future__ import annotations

import abc
import re

from jb_declarative_formatters.parsers.cpp_parser import CppParser


def mangle_intrinsic_name(name: str, parameters_count: int) -> str:
    return f"__{name}___{parameters_count}__"


def create_intrinsic(intrinsic_overloads: dict[str, int],
                     mangled_name: str, base_name: str, expression: str, optional: bool,
                     parameters: list[TypeVizIntrinsicParameter],
                     dependencies: list[str],
                     intrinsic_unique_id: int):
    if intrinsic_overloads[mangled_name] == 1:
        return TypeVizIntrinsicInlined(
            mangled_name, base_name, expression,
            optional, parameters,
            dependencies, intrinsic_unique_id)
    else:
        return TypeVizIntrinsicLambdaBased(
            mangled_name, base_name, expression,
            optional, parameters,
            dependencies, intrinsic_unique_id)


class TypeVizIntrinsicParameter(object):
    def __init__(self, parameter_name: str | None, parameter_type: str):
        self.parameter_type = parameter_type
        self.parameter_name = parameter_name


class IntrinsicsScope:
    def __init__(self, sorted_list: list[TypeVizIntrinsic], intrinsic_scope_name: str):
        self.sorted_list = sorted_list
        self.intrinsic_scope_name = intrinsic_scope_name
        self.name_to_indexes_map: dict[str, list[int]] = {}

        for i in range(len(sorted_list)):
            name = sorted_list[i].name
            if name not in self.name_to_indexes_map:
                self.name_to_indexes_map[name] = list[int]()
            self.name_to_indexes_map[name].append(i)

    def retain_only_lazy(self) -> IntrinsicsScope:
        new_list = [item for item in self.sorted_list if item.is_lazy]
        return IntrinsicsScope(new_list, self.intrinsic_scope_name)


class TypeVizIntrinsic(abc.ABC):
    INTRINSIC_NAME_PREFIX = "JB_INTRINSIC_MACRO_"

    def __init__(self, mangled_name: str, base_name: str, expression: str, optional: bool,
                 parameters: list[TypeVizIntrinsicParameter],
                 dependencies: list[str],
                 intrinsic_unique_id: int):
        self.parameters = parameters
        self.base_name: str = base_name
        self.optional = optional
        self.name: str = mangled_name
        self.expression: str = expression
        self.original_expression: str = expression
        self.is_used = False
        self.unique_dependencies = set(dependencies)
        self.is_lazy = True
        self.intrinsic_unique_id = intrinsic_unique_id

    def __hash__(self):
        return hash((self.original_expression, self.name, self.optional))

    def change_expression(self, new_expression: str):
        self.expression = new_expression

    def mark_as_used(self):
        self.is_used = True

    @abc.abstractmethod
    def get_intrinsic_call_replacement(self,
                                       expression: str, intrinsic_call: CppParser.FunctionCall,
                                       intrinsic_scope_name: str) -> tuple[str, int, int]:
        pass

    @abc.abstractmethod
    def get_code_for_validate(self, prolog: str) -> str:
        pass

    @abc.abstractmethod
    def get_definition_code(self) -> str:
        pass


class TypeVizIntrinsicInlined(TypeVizIntrinsic):

    def __init__(self, mangled_name: str, base_name: str, expression: str, optional: bool,
                 parameters: list[TypeVizIntrinsicParameter],
                 dependencies: list[str],
                 intrinsic_unique_id: int):
        super().__init__(mangled_name, base_name, expression, optional, parameters, dependencies, intrinsic_unique_id)
        self.is_lazy = False

    def get_intrinsic_call_replacement(self,
                                       expression: str, intrinsic_call: CppParser.FunctionCall,
                                       intrinsic_scope_name: str) -> tuple[str, int, int]:
        name_len = len(self.base_name)
        start_pos = intrinsic_call.args_begin_pos - name_len - 1
        end_pos = intrinsic_call.args_end_pos

        init_params = []
        fixed_expression = self.expression
        for index, param in enumerate(self.parameters):
            # we should keep even unused parameters because an argument can be a function call with side effects
            param_name = param.parameter_name or f"__$jb$unused${index}"
            arg_expression = intrinsic_call.args[index]
            param_regex = re.compile(rf"\b{re.escape(param_name)}\b")
            type_must_be_deduced = "auto" in param.parameter_type
            argument_can_be_inlined = CppParser.is_literal_expr(arg_expression) or CppParser.is_identifier(arg_expression)
            if not type_must_be_deduced and argument_can_be_inlined:
                # if the argument is a trivial literal or identifier, we may inline it as is
                fixed_expression = param_regex.sub(f"(({param.parameter_type}){arg_expression})", fixed_expression)
            else:
                unique_param_name = f"{param_name}_{self.intrinsic_unique_id}"
                init_params.append(f"{param.parameter_type} {unique_param_name} = {arg_expression};")
                fixed_expression = param_regex.sub(unique_param_name, fixed_expression)

        init_params_block = ''.join(init_params)
        if init_params_block:
            text = f"({{" \
                   f"/*intrinsic {intrinsic_scope_name}:{self.base_name}*/" \
                   f"{init_params_block}" \
                   f"({fixed_expression});" \
                   f"}})"
        else:
            # TODO: If we commonize the code, it will break [CppParser.simplify_cpp_expression]
            #  because it doesn't support statement expression simplification {(expressions in curly brackets);}
            text = f"(" \
                   f"/*intrinsic {intrinsic_scope_name}:{self.base_name}*/" \
                   f'{fixed_expression}' \
                   f")"

        return text, start_pos, end_pos

    def get_definition_code(self) -> str:
        return ''

    def get_code_for_validate(self, prolog: str) -> str:
        return ''


class TypeVizIntrinsicLambdaBased(TypeVizIntrinsic):

    def __init__(self, mangled_name: str, base_name: str, expression: str, optional: bool,
                 parameters: list[TypeVizIntrinsicParameter],
                 dependencies: list[str],
                 intrinsic_unique_id: int):
        super().__init__(mangled_name, base_name, expression, optional, parameters, dependencies, intrinsic_unique_id)

    def get_intrinsic_call_replacement(self,
                                       expression: str, intrinsic_call: CppParser.FunctionCall,
                                       intrinsic_scope_name: str) -> tuple[str, int, int]:
        name_len = len(self.base_name)
        start_pos = intrinsic_call.args_begin_pos - name_len - 1
        end_pos = intrinsic_call.args_begin_pos
        text = f"{self.INTRINSIC_NAME_PREFIX}{self.name}("

        return text, start_pos, end_pos

    def get_code_for_validate(self, prolog: str) -> str:
        param_str = ", ".join([f"{p.parameter_type} {p.parameter_name}" for p in self.parameters])
        lambda_stmt = f"[&]({param_str})" \
                      "{" \
                      f" {prolog} " \
                      f" return {self.expression} ;" \
                      "}"
        return lambda_stmt

    def get_definition_code(self) -> str:
        param_str_without_types = ", ".join([f" {p.parameter_name}" for p in self.parameters])
        expr = self.expression
        for param in self.parameters:
            expr = expr.replace(param.parameter_name, f'(({param.parameter_type}) ({param.parameter_name}))')
        expr = expr.replace("\n", "\\\n")
        macros = f"\n" \
                 f"#define {self.INTRINSIC_NAME_PREFIX}{self.name}({param_str_without_types}) " \
                 f" ( {expr} )\n" \
                 f""
        return macros


class BuiltinIntrinsics:
    @staticmethod
    def _create_builtin_intrinsic(base_name: str, parameters: list[TypeVizIntrinsicParameter], expression: str):
        name = mangle_intrinsic_name(base_name, len(parameters))
        return TypeVizIntrinsicInlined(name, base_name, expression, False, parameters, [], 0)

    _BUILTIN_INTRINSICS = [
        _create_builtin_intrinsic(
            base_name="__findnonnull",
            parameters=[
                TypeVizIntrinsicParameter("__$jb$param$ptr", "auto *"),
                TypeVizIntrinsicParameter("__$jb$param$size", "auto")
            ],
            expression="({ "
                       "int __$jb$result = -1; "
                       "for (int i = 0; i < __$jb$param$size; ++ i) {"
                       " if (__$jb$param$ptr[i] != nullptr) { __$jb$result = i; break; }"
                       "} "
                       "__$jb$result; "
                       "})")
    ]

    _BUILTIN_SCOPE = IntrinsicsScope(_BUILTIN_INTRINSICS, "builtin")

    @classmethod
    def get_scope(cls):
        return cls._BUILTIN_SCOPE
