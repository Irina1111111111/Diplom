import re
from enum import Enum, auto
from typing import List, Tuple, Optional, Callable, Dict, Set
import hashlib

from . import TypeVizItemProviderArrayItems, TypeVizItemProviderIndexListItems, TypeVizItemProviderSingle, \
    TypeVizItemIndexNodeTypeNode, TypeVizItemValuePointerTypeNode, TypeVizItemProviderLinkedListItems, \
    TypeVizItemProviderTreeItems, TypeVizItemProviderExpanded
from .type_viz import TypeVizSmartPointer, TypeViz, TypeVizName, TypeVizStringView
from .type_viz_intrinsic import TypeVizIntrinsic, IntrinsicsScope
from .type_viz_mixins import TypeVizItemSyntheticGetterNodeMixin, TypeVizItemNamedNodeMixin
from .parsers.cpp_parser import CppParser
from .type_viz_synthetic_method import SyntheticMethod, SyntheticMethodDefinition
from .type_viz_type_traits import TypeVizTypeTraits


class TypeVizTopLevelMethods:
    # An easy way to disable everything if something goes wrong
    DISABLE_TOP_LEVEL_DECLARATIONS = False
    ENABLE_ASSERTIONS = False

    _IndexableTypeItemProvider = TypeVizItemProviderArrayItems | TypeVizItemProviderIndexListItems
    _SingleTypeItemProvider = TypeVizItemProviderSingle | TypeVizItemProviderExpanded
    _IndexableItemNode = TypeVizItemValuePointerTypeNode | TypeVizItemIndexNodeTypeNode

    _INVALID_CHAR_REGEX = re.compile(r'[^\w$]')

    _INTERNAL_INTRINSIC_PREFIX = "_jb$intrinsic$internal$"

    class _SubscriptStatus(Enum):
        REQUIRED = auto()
        ALREADY_EXISTS = auto()
        FORBIDDEN = auto()

    class _TypeName:
        def __init__(self, name: str, has_wildcards: bool):
            self.name = name
            self.has_wildcards = has_wildcards

        def __str__(self) -> str:
            return self.name

    def __init__(self):
        self._known_method_names: Dict[str, Dict[str, Dict[str, int]]] = {}
        self._known_intrinsics: Set[Tuple[str, str, str]] = set()
        self._private_getters: Dict[str, str] = {}
        self._subscript_operators_in_types: Dict[str, str] = {}
        self._method_definitions: List[SyntheticMethodDefinition] = []

    @property
    def methods_definitions(self) -> List[SyntheticMethodDefinition]:
        return self._method_definitions

    def collect_top_level_methods_from(self, type_regex: str, type_viz: TypeViz, type_viz_name: TypeVizName):
        if self.DISABLE_TOP_LEVEL_DECLARATIONS:
            return

        type_name = self._TypeName(self._fix_type_regex(type_regex), type_viz_name.has_wildcard)

        self._add_global_intrinsics(type_viz.global_intrinsics)
        self._add_type_intrinsics(type_name, type_viz.type_intrinsics)
        item_providers = type_viz.item_providers or []
        string_methods = None
        for item_provider in item_providers:
            match item_provider:
                case TypeVizItemProviderSingle():
                    self._add_single_item_getter(type_name, item_provider)
                case TypeVizItemProviderExpanded():
                    self._add_single_item_getter(type_name, item_provider)
                case TypeVizItemProviderArrayItems():
                    self._add_array_methods(type_name, item_provider)
                    if not string_methods:
                        string_methods = self._string_methods_from_array_items(type_name, item_provider)
                        self._method_definitions += string_methods
                case TypeVizItemProviderIndexListItems():
                    self._add_index_list_methods(type_name, item_provider)
                case TypeVizItemProviderLinkedListItems():
                    self._add_linked_list_method(type_name, item_provider)
                case TypeVizItemProviderTreeItems():
                    self._add_tree_method(type_name, item_provider)

        if type_viz.smart_pointer is not None:
            self._method_definitions += self._smart_pointer_methods(type_name, type_viz.smart_pointer)
        if not string_methods:
            self._method_definitions += self._string_methods_from_string_view(type_name, type_viz.string_views)

    @staticmethod
    def _fix_type_regex(type_regex: str) -> str:
        type_regex = type_regex.removeprefix("^").removesuffix("$")
        while True:
            fixed_type_regex = type_regex.replace(">>", "> >")
            if fixed_type_regex == type_regex:
                return fixed_type_regex
            type_regex = fixed_type_regex

    @staticmethod
    def _make_internal_name(name: str) -> str:
        return f"jb$internal$name$${name}$$"

    @classmethod
    def _mangle_name(cls, name: str) -> str:
        return cls._INVALID_CHAR_REGEX.sub('$', name)

    @classmethod
    def _prepare_expr(cls, expr: str) -> str:
        expr = expr.replace(TypeVizIntrinsic.INTRINSIC_NAME_PREFIX, cls._INTERNAL_INTRINSIC_PREFIX)
        # %1 - is a full type.
        # %2, %3, ... - are regex matches inside the type template
        # replace $T1 (i = 0) -> %2, $T2 (i = 1) -> %3, $T3 (i = 2) -> %4, ...
        substituted_expr, all_substituted = CppParser.substitute_wildcards(expr, lambda i: f"%{i + 2}")
        assert all_substituted, f"There are unsubstituted wildcards left in the expression '{substituted_expr}'"
        return substituted_expr

    def _add_getter_with_unique_name(self, type_name: _TypeName, item_node: TypeVizItemSyntheticGetterNodeMixin,
                                     method_name: str, method_expr: str) -> bool:
        method_expressions = self._known_method_names.setdefault(type_name.name, {}).setdefault(method_name, {})
        new_method_name_id = len(method_expressions)
        method_name_id = method_expressions.setdefault(method_expr, new_method_name_id)
        if method_name_id:
            method_name += str(method_name_id)
        if item_node.synthetic_getter is None:
            item_node.synthetic_getter = SyntheticMethod.named_method(method_name)
        elif self.ENABLE_ASSERTIONS:
            assert item_node.synthetic_getter.name == method_name, \
                f"getter name: {item_node.synthetic_getter.name}, generated name: {method_name}"
        return new_method_name_id == method_name_id

    def _try_declare_subscript_operator(self, type_name: _TypeName, method_expr: str) -> _SubscriptStatus:
        declared = self._subscript_operators_in_types.get(type_name.name, None)
        if declared is None:
            self._subscript_operators_in_types[type_name.name] = method_expr
            return self._SubscriptStatus.REQUIRED
        return self._SubscriptStatus.ALREADY_EXISTS if declared == method_expr else self._SubscriptStatus.FORBIDDEN

    @staticmethod
    def _join_operator_regex_and_declarations(type_name: _TypeName, operators: List[Tuple[str, str]]) -> \
            List[SyntheticMethodDefinition]:
        if type_name.has_wildcards:
            operator_names_regex = "|".join(map(lambda op: re.escape(op[0]), operators))
            operator_names_regex = f"^({type_name})::operator(:?{operator_names_regex})$"
            operator_declarations = "\n".join(map(lambda op: op[1], operators))
            return [SyntheticMethodDefinition(operator_names_regex, operator_declarations, True)]
        definitions = []
        for op, decl in operators:
            operator_name = f"{type_name}::operator{op}"
            operator_body = decl.replace("%1", type_name.name)
            definitions.append(SyntheticMethodDefinition(operator_name, operator_body, False))
        return definitions

    def _smart_pointer_methods(self, type_name: _TypeName, smart_pointer: TypeVizSmartPointer) -> \
            List[SyntheticMethodDefinition]:
        expr = self._prepare_expr(smart_pointer.expression.text)
        methods = self._join_operator_regex_and_declarations(type_name, self._get_minimal_operators(expr))
        # Declare 'Indexable' separately because LLDB may be unable to compile methods like 'operator+', 'operator-'
        # since it requires an available copy constructor for a smart pointer type (usually, 'Indexable' is an iterator)
        if smart_pointer.usage == TypeVizSmartPointer.Usage.Indexable:
            indexable_operators = self._get_indexable_operators(type_name, expr)
            if indexable_operators:
                methods += self._join_operator_regex_and_declarations(type_name, indexable_operators)

        return methods

    @classmethod
    def _get_minimal_operators(cls, expr: str) -> List[Tuple[str, str]]:
        other_param = cls._make_internal_name("other")
        return [
            ("->", f"auto %1::operator->() const -> decltype({expr}) {{ return {expr}; }}"),
            ("*", f"auto %1::operator*() const -> decltype((*({expr}))) {{ return (*({expr})); }}"),
            ("!", f"bool %1::operator!() const {{ return !({expr}); }}"),
            ("==", f"bool %1::operator==(const ::%1 &{other_param}) const {{ "
                   f"return ({expr}) == {other_param}.operator->(); "
                   f"}}"),
            ("!=", f"bool %1::operator!=(const ::%1 &{other_param}) const {{ "
                   f"return ({expr}) != {other_param}.operator->(); "
                   f"}}")
        ]

    def _get_indexable_operators(self, type_name: _TypeName, expr: str) -> List[Tuple[str, str]]:
        index_param = self._make_internal_name("index")
        operators = []
        subscript_body = f"return (({expr})[{index_param}]);"
        # Add an operator even if the status is FORBIDDEN.
        # This declaration is not used as a synthetic getter, therefore it is safe to add one more declaration here.
        # In the worst case, it will simply be ignored as a redeclaration.
        if self._try_declare_subscript_operator(type_name, subscript_body) != self._SubscriptStatus.ALREADY_EXISTS:
            operators.append(
                ("[]", f"decltype(auto) %1::operator[](long long {index_param}) const {{ {subscript_body} }}")
            )
        # Support only trivial expressions with the property accessing, such as '_Ptr'.
        # Do not support complex expressions, like:
        #   '_Ptr + _Idx' or
        #   '_Ptr->_Isnil ? nullptr : &_Ptr->_Myval' (Usually, such expressions are used for 'Minimal' smart pointers)
        if CppParser.is_trivial_expression(expr):
            offset_param = self._make_internal_name("offset")
            result = self._make_internal_name("result")
            operators += [
                ("+", f"::%1 %1::operator+(long long {offset_param}) const {{ "
                      f"%1 {result} = *this; {result}.{expr} += {offset_param}; return {result}; "
                      f"}}"),
                ("-", f"::%1 %1::operator-(long long {offset_param}) const {{ "
                      f"%1 {result} = *this; {result}.{expr} -= {offset_param}; return {result}; "
                      f"}}")
            ]
        return operators

    @classmethod
    def _indexable_node_expression(cls, item_node: _IndexableItemNode, index_param: str) -> str:
        expr = cls._prepare_expr(item_node.expr.text)
        if isinstance(item_node, TypeVizItemIndexNodeTypeNode):
            return expr.replace("$i", index_param)
        return f"({expr})[{index_param}]"

    @classmethod
    def _subscript_operator_body(cls, item_nodes: List[_IndexableItemNode], index_param: str) -> str:
        lines = []
        for (index, item_node) in enumerate(item_nodes):
            expr = cls._indexable_node_expression(item_node, index_param)
            ignore_condition = index + 1 == len(item_nodes)
            if not ignore_condition and item_node.condition is not None and item_node.condition.condition:
                condition_expr = cls._prepare_expr(item_node.condition.condition)
                if isinstance(item_node, TypeVizItemIndexNodeTypeNode):
                    condition_expr = condition_expr.replace("$i", index_param)
                lines.append(f"if ({condition_expr}) ")
                lines.append(f"return ({expr});\n")
            else:
                lines.append(f"return ({expr});\n")
                return "".join(lines)

        return "".join(lines)

    @classmethod
    def _make_mutable_const_method(cls, type_name: _TypeName, method_name: str, body: str,
                                   params: Optional[List[Tuple[str, str]]] = None,
                                   mutable_method_prefix: Optional[str] = None) -> SyntheticMethodDefinition:
        params = params or []
        param_list = ', '.join(map(lambda p: f"{p[0]} {p[1]}", params))
        arg_list = ', '.join(map(lambda p: f"{p[1]}", params))
        mutable_method = cls._make_internal_name(f"{mutable_method_prefix or method_name}$mutable")
        if type_name.has_wildcards:
            return SyntheticMethodDefinition(
                full_name=f"^({type_name})::{re.escape(method_name)}$",
                body_substitution=f"decltype(auto) %1::{mutable_method}({param_list}) {{ {body} }}\n"
                                  f"decltype(auto) %1::{method_name}({param_list}) const {{ "
                                  f"return const_cast<::%1 *>(this)->{mutable_method}({arg_list}); "
                                  f"}}",
                name_uses_regex=True
            )
        return SyntheticMethodDefinition(
            full_name=f"{type_name}::{method_name}",
            body_substitution=f"decltype(auto) {type_name}::{mutable_method}({param_list}) {{ {body} }}\n"
                              f"decltype(auto) {type_name}::{method_name}({param_list}) const {{ "
                              f"return const_cast<::{type_name} *>(this)->{mutable_method}({arg_list}); "
                              f"}}",
            name_uses_regex=False
        )

    @classmethod
    def _container_method_definition(cls, type_name: _TypeName, item_node: TypeVizItemSyntheticGetterNodeMixin,
                                     body: str, index_param: str, mutable_method_prefix: Optional[str] = None) -> \
            SyntheticMethodDefinition:
        method_name = item_node.synthetic_getter.name
        params = [("long long", index_param)]
        return cls._make_mutable_const_method(type_name, method_name, body, params, mutable_method_prefix)

    def _add_indexed_methods(self, type_name: _TypeName, indexable_provider: _IndexableTypeItemProvider,
                             get_nodes: Callable[[_IndexableTypeItemProvider], List[_IndexableItemNode]]):
        index_param = self._make_internal_name("index")
        item_nodes = get_nodes(indexable_provider)
        item_nodes_count = len(item_nodes)
        if item_nodes_count == 1 or TypeVizTypeTraits.is_subscript_operator_required(type_name.name):
            body = self._subscript_operator_body(item_nodes, index_param)
            subscript_status = self._try_declare_subscript_operator(type_name, body)
            if subscript_status != self._SubscriptStatus.FORBIDDEN:
                if indexable_provider.synthetic_getter is None:
                    indexable_provider.synthetic_getter = SyntheticMethod.subscript_operator()
                elif self.ENABLE_ASSERTIONS:
                    assert isinstance(indexable_provider.synthetic_getter.identifier,
                                      SyntheticMethod.SubscriptOperator)
                if subscript_status == self._SubscriptStatus.REQUIRED:
                    method_definition = self._container_method_definition(type_name, indexable_provider, body,
                                                                          index_param, "op$subscript")
                    self._method_definitions.append(method_definition)
                return

        for item_node in item_nodes:
            expr = self._indexable_node_expression(item_node, index_param)
            if self._add_getter_with_unique_name(type_name, item_node, f"_get$", expr):
                body = f"return ({expr});"
                method_definition = self._container_method_definition(type_name, item_node, body, index_param)
                self._method_definitions.append(method_definition)

    def _add_array_methods(self, type_name: _TypeName, array_items: TypeVizItemProviderArrayItems):
        self._add_indexed_methods(type_name, array_items, lambda items: items.value_pointer_nodes)

    def _add_index_list_methods(self, type_name: _TypeName, index_list_items: TypeVizItemProviderIndexListItems):
        self._add_indexed_methods(type_name, index_list_items, lambda items: items.value_node_nodes)

    def _try_as_internal_getter(self, name: str, expr: str) -> str:
        expr = CppParser.simplify_cpp_expression(expr)
        expr = self._prepare_expr(expr)
        if CppParser.is_trivial_expression(expr):
            return expr
        getter = f"private$get${name}${hashlib.sha256(expr.encode()).hexdigest()}"
        getter = self._make_internal_name(getter)
        if getter not in self._private_getters:
            type_name = self._TypeName("(.*)", True)
            self._method_definitions.append(self._make_mutable_const_method(type_name, getter, f"return ({expr});"))
            self._private_getters[getter] = expr
        elif self.ENABLE_ASSERTIONS:
            assert self._private_getters[getter] == expr

        return f"{getter}()"

    def _add_linked_list_method(self, type_name: _TypeName, list_items: TypeVizItemProviderLinkedListItems):
        index_param = self._make_internal_name("index")
        next_ptr = self._try_as_internal_getter("list$next", list_items.next_pointer_node.text)
        get_value = self._try_as_internal_getter("list$value", list_items.value_node_node.expr.text)
        body = (f"auto it = {self._prepare_expr(list_items.head_pointer_node.text)};\n"
                f"while ({index_param}-- > 0) it = it->{next_ptr};\n"
                f"return (it->{get_value});\n")
        if self._add_getter_with_unique_name(type_name, list_items, f"_get$", body):
            method_definition = self._container_method_definition(type_name, list_items, body, index_param)
            self._method_definitions.append(method_definition)

    def _add_tree_method(self, type_name: _TypeName, tree_items: TypeVizItemProviderTreeItems):
        index_param = self._make_internal_name("index")
        counter = self._make_internal_name("element_counter")
        node = self._make_internal_name("node")
        found = self._make_internal_name("found")
        inorder_method = self._make_internal_name("get_inorder_element")
        node_ptr_type = self._make_internal_name("NodePtr")
        inorder_helper_type = self._make_internal_name("InorderHelper")

        head_ptr = self._prepare_expr(tree_items.head_pointer_node.text)
        left_ptr = self._try_as_internal_getter("tree$left", tree_items.left_pointer_node.text)
        right_ptr = self._try_as_internal_getter("tree$right", tree_items.right_pointer_node.text)
        get_value = self._try_as_internal_getter("tree$value", tree_items.value_node_node.expr.text)
        stop_condition = f"(!{node})"
        if tree_items.value_node_node.condition and tree_items.value_node_node.condition.condition:
            condition = tree_items.value_node_node.condition.condition
            condition_expr = self._try_as_internal_getter("tree$condition", condition)
            stop_condition += f" || !({node}->{condition_expr})"

        body = (f"using {node_ptr_type} = decltype({head_ptr});\n"
                f"struct {inorder_helper_type} {{\n"
                f"static {node_ptr_type} {inorder_method}({node_ptr_type} {node}, long long &{counter}) {{\n"
                f"if ({stop_condition}) return nullptr;\n"
                f"if (auto {found} = {inorder_method}({node}->{left_ptr}, {counter})) return {found};\n"
                f"if ({counter}-- <= 0) return {node};\n"
                f"return {inorder_method}({node}->{right_ptr}, {counter});"
                f"}}\n"
                f"}};\n"
                f"return ({inorder_helper_type}::{inorder_method}({head_ptr}, {index_param})->{get_value});\n")

        if self._add_getter_with_unique_name(type_name, tree_items, f"_get$", body):
            method_definition = self._container_method_definition(type_name, tree_items, body, index_param)
            self._method_definitions.append(method_definition)

    def _add_single_item_getter(self, type_name: _TypeName, single_item: _SingleTypeItemProvider):
        expr = CppParser.simplify_cpp_expression(single_item.expr.text)
        if CppParser.is_trivial_expression(expr):
            return
        specifier, sub_expression = CppParser.cut_deref_or_address_of_from_trivial_expression(expr)
        if specifier and sub_expression:
            return
        expr = self._prepare_expr(expr)
        item_name = single_item.name if isinstance(single_item, TypeVizItemNamedNodeMixin) else None
        method_name = "_expanded$" if item_name is None else f"_item${self._mangle_name(item_name)}$"
        if self._add_getter_with_unique_name(type_name, single_item, method_name, expr):
            unique_method_name = single_item.synthetic_getter.name
            method_definition = self._make_mutable_const_method(type_name, unique_method_name, f"return ({expr});")
            self._method_definitions.append(method_definition)

    def _add_global_intrinsics(self, global_intrinsics: IntrinsicsScope):
        for intrinsic in reversed(global_intrinsics.sorted_list):
            if not intrinsic.is_used or not intrinsic.is_lazy:
                continue
            key = ('', intrinsic.name, intrinsic.expression)
            if key in self._known_intrinsics:
                continue
            self._known_intrinsics.add(key)
            name = f"{self._INTERNAL_INTRINSIC_PREFIX}{intrinsic.name}"
            params = ', '.join(map(lambda p: f"{p.parameter_type} {p.parameter_name or ''}", intrinsic.parameters))
            expr = self._prepare_expr(intrinsic.expression)
            method = SyntheticMethodDefinition(name, f"decltype(auto) {name}({params}) {{ return {expr}; }}", False)
            self._method_definitions.append(method)

    def _add_type_intrinsics(self, type_name: _TypeName, type_intrinsics: IntrinsicsScope):
        for intrinsic in reversed(type_intrinsics.sorted_list):
            if not intrinsic.is_used or not intrinsic.is_lazy:
                continue
            key = (type_name.name, intrinsic.name, intrinsic.expression)
            if key in self._known_intrinsics:
                continue
            self._known_intrinsics.add(key)
            expr = self._prepare_expr(intrinsic.expression)
            name = f"{self._INTERNAL_INTRINSIC_PREFIX}{intrinsic.name}"
            params = [(param.parameter_type, param.parameter_name or '') for param in intrinsic.parameters]
            self._method_definitions.append(self._make_mutable_const_method(type_name, name, f"return {expr};", params))

    @classmethod
    def _string_methods(cls, type_name: _TypeName,
                        init_block_builder: Callable[[TypeVizTypeTraits.StringTraits, str, str], str]) -> \
            List[SyntheticMethodDefinition]:
        string_type_traits = TypeVizTypeTraits.get_string_type_traits(type_name.name)
        if not string_type_traits:
            return []

        methods = []

        self_size = cls._make_internal_name("self$size")
        self_data = cls._make_internal_name("self$data")
        other_data = cls._make_internal_name("other$data")
        other_size = cls._make_internal_name("other$size")

        for type_specialization, type_traits in string_type_traits:
            init_part = init_block_builder(type_traits, self_data, self_size)

            def make_compare_part(is_equal: bool) -> str:
                op = "==" if is_equal else "!="
                # In Unreal natvis, empty strings are interpreted as a string "\0" with a size equal to 1
                unreal_empty_string_hack = f"if ({self_size} == 1 && {self_data} && !*{self_data}) {self_size} = 0;"
                return (
                    f"{unreal_empty_string_hack}\n"
                    f"if (!{other_data}) return {self_size} {op} 0;\n"
                    f"const unsigned long long {other_size} = {type_traits.strlen}({other_data});\n"
                    f"if (!{self_data}) return {other_size} {op} 0;\n"
                    f"if ({other_size} != {self_size}) return {'false' if is_equal else 'true'};\n"
                    f"return {type_traits.strncmp}({self_data}, {other_data}, {self_size}) {op} 0;"
                )

            type_name_specialization = cls._TypeName(type_specialization, type_name.has_wildcards)
            # Add operators separately so that compilation and evaluation are faster for each of them
            methods += cls._join_operator_regex_and_declarations(
                type_name_specialization,
                [("==", (f"bool %1::operator==(const {type_traits.char_type} *{other_data}) const {{\n"
                         f"{init_part}\n"
                         f"{make_compare_part(True)}\n"
                         f"}}"))])
            methods += cls._join_operator_regex_and_declarations(
                type_name_specialization,
                [("!=", (f"bool %1::operator!=(const {type_traits.char_type} *{other_data}) const {{\n"
                         f"{init_part}\n"
                         f"{make_compare_part(False)}\n"
                         f"}}"))])

        return methods

    @classmethod
    def _string_methods_from_array_items(cls, type_name: _TypeName, array_items: TypeVizItemProviderArrayItems) -> \
            List[SyntheticMethodDefinition]:
        def array_items_init_block(type_traits: TypeVizTypeTraits.StringTraits, self_data: str, self_size: str) -> str:
            lines = [
                f"unsigned long long {self_size} = 0;\n"
                f"const {type_traits.char_type} *{self_data} = nullptr;\n"
            ]
            for size_node in array_items.size_nodes:
                if size_node.condition is not None and size_node.condition.condition:
                    lines.append(f"if ({cls._prepare_expr(size_node.condition.condition)}) ")
                lines.append(f"{self_size} = (unsigned long long)({cls._prepare_expr(size_node.text)});\n")

            for pointer_node in array_items.value_pointer_nodes:
                if pointer_node.condition is not None and pointer_node.condition.condition:
                    lines.append(f"if ({cls._prepare_expr(pointer_node.condition.condition)}) ")
                data_pointer_expr = cls._prepare_expr(pointer_node.expr.text)
                lines.append(f"{self_data} = (const {type_traits.char_type} *)({data_pointer_expr});\n")

            return "".join(lines)

        return cls._string_methods(type_name, array_items_init_block)

    @classmethod
    def _string_methods_from_string_view(cls, type_name: _TypeName, string_views: List[TypeVizStringView]) -> \
            List[SyntheticMethodDefinition]:
        if not string_views:
            return []

        def string_view_init_block(type_traits: TypeVizTypeTraits.StringTraits, self_data: str, self_size: str) -> str:
            lines = [
                f"unsigned long long {self_size} = (unsigned long long)(-1);\n"
                f"const {type_traits.char_type} *{self_data} = nullptr;\n"
            ]
            for string_view in string_views:
                if string_view.condition is not None and string_view.condition.condition:
                    lines.append(f"if ({cls._prepare_expr(string_view.condition.condition)})\n")
                lines.append("{\n")
                data_pointer_expr = cls._prepare_expr(string_view.expression.text)
                lines.append(f"{self_data} = (const {type_traits.char_type} *)({data_pointer_expr});\n")
                string_len_expr = string_view.expression.view_options.array_size
                if string_len_expr:
                    lines.append(f"{self_size} = (unsigned long long)({cls._prepare_expr(string_len_expr)});\n")
                lines.append("}\n")
            lines.append(f"if ({self_size} == (unsigned long long)(-1)) "
                         f"{self_size} = {self_data} ? {type_traits.strlen}({self_data}) : 0;\n")
            return "".join(lines)

        return cls._string_methods(type_name, string_view_init_block)
