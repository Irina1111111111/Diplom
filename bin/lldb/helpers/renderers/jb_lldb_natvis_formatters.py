from __future__ import annotations

import re
from typing import List, Tuple, Sequence, Any, Callable

from jb_declarative_formatters import *
from jb_declarative_formatters.parsers.cpp_parser import CppParser
from jb_declarative_formatters.type_name_template import TypeNameTemplate
from jb_declarative_formatters.type_viz_expression import TypeVizFormatOptions, TypeVizCondition
from jb_declarative_formatters.type_viz_item_nodes import TypeVizItemExecCodeBlockTypeNode, \
    TypeVizItemItemCodeBlockTypeNode, TypeVizItemIfCodeBlockTypeNode, TypeVizItemElseCodeBlockTypeNode, \
    TypeVizItemElseIfCodeBlockTypeNode, TypeVizItemLoopCodeBlockTypeNode, TypeVizItemBreakCodeBlockTypeNode, \
    TypeVizItemVariableTypeNode
from jb_declarative_formatters.type_viz_synthetic_method import SyntheticMethod
from renderers.jb_lldb_builtin_formatters import StructChildrenProvider
from renderers.jb_lldb_declarative_formatters_options import *
from renderers.jb_lldb_evaluation_utils import resolve_type_wildcards
from renderers.jb_lldb_format import overlay_child_format, update_value_dynamic_state, overlay_summary_format
from renderers.jb_lldb_format_specs import *
from renderers.jb_lldb_logging import get_suppress_errors
from renderers.jb_lldb_utils import *

g_cache_variables_initialized: dict[str, bool] = dict()


class NatVisDescriptor(AbstractVisDescriptor):
    def __init__(self, candidates: List[Tuple[TypeViz, TypeVizName]], name_template: TypeNameTemplate):
        self.type_name_template = name_template
        self.viz_candidates = [(viz, viz_name, _match_type_viz_template(viz_name.type_name_template, name_template)) for
                               viz, viz_name in candidates]

    def output_summary(self, value_non_synth: lldb.SBValue, stream: Stream):

        for name_viz_pair in self.viz_candidates:
            viz, type_viz_name, matches = name_viz_pair
            IntrinsicsPrologCache.update_current_intrinsics_scope(
                global_intrinsic_scope=viz.global_intrinsics,
                type_intrinsic_scope=viz.type_intrinsics,
                type_wildcards=matches)
            try:
                log("Trying visualizer for type '{}'...", str(type_viz_name))
                if not _check_include_exclude_view_condition(viz, value_non_synth):
                    continue

                if not viz.summaries:
                    log('No user provided summary found, return default...')
                    return self.output_summary_from_children(value_non_synth, stream)

                # try to choose candidate from ordered display string expressions
                success = _find_first_good_node(_process_summary_node, viz.summaries, value_non_synth,
                                                matches, stream)
                if success is not None:
                    return

            except EvaluateError:
                continue
            finally:
                IntrinsicsPrologCache.rollback_current_intrinsics_scope()

        log("No matching display string candidate found, fallback to default")
        return self.output_summary_from_children(value_non_synth, stream)

    def output_summary_from_children(self, value_non_synth, stream):
        children_provider = self.prepare_children(value_non_synth)
        num_children = children_provider.num_children()

        stream.output("{")
        if stream.length > get_max_string_length():
            stream.output('...')
        elif num_children == 0:
            stream.output('...')
        else:
            for child_index in range(num_children):
                child: lldb.SBValue = children_provider.get_child_at_index(child_index)
                child_non_synth = child.GetNonSyntheticValue()
                child_name = child_non_synth.GetName() or ''
                if child_name == RAW_VIEW_ITEM_NAME:
                    continue
                if child_index != 0:
                    stream.output(", ")

                if child_index > 2 or stream.length > get_max_string_length():
                    stream.output("...")
                    break

                stream.output(child_name)
                stream.output("=")
                if stream.length > get_max_string_length():
                    stream.output("...")
                    break

                stream.output_object(child_non_synth)

        stream.output("}")

    def prepare_children(self, value_non_synth: lldb.SBValue):
        value_name = value_non_synth.GetName()
        value_type_name = value_non_synth.GetType().GetName()
        log("Initial retrieving children of value named '{}' of type '{}'...", value_name, value_type_name)

        viz = None
        providers = None
        start_indexes = None
        matches: Tuple[str, ...] = ()
        level = get_recursion_level()
        if level >= g_max_recursion_level - 1:
            log("Natvis visualizer for type '{}' of value '{}' has been ignored: "
                "recursion level exceeds the maximum supported limit of {}",
                value_type_name, value_name, g_max_recursion_level)
        else:
            for name_viz_pair in self.viz_candidates:
                viz, type_viz_name, matches = name_viz_pair
                try:
                    if not _check_include_exclude_view_condition(viz, value_non_synth):
                        continue
                    else:
                        IntrinsicsPrologCache.update_current_intrinsics_scope(
                            global_intrinsic_scope=viz.global_intrinsics,
                            type_intrinsic_scope=viz.type_intrinsics,
                            type_wildcards=matches)
                        try:
                            set_recursion_level(level + 1)
                            providers = _try_create_child_providers(value_non_synth, viz, type_viz_name, self.type_name_template)
                            if providers:
                                start_indexes = _calculate_child_providers_start_indexes(providers)
                        finally:
                            set_recursion_level(level)
                            IntrinsicsPrologCache.rollback_current_intrinsics_scope()

                except EvaluateError as error:
                    log("Error occurred: {}", error)
                    continue
                break

        if providers is None:
            log("No child provider found for '{}'", value_non_synth.GetType().GetName())
            return StructChildrenProvider(value_non_synth)

        return NatVisChildrenProvider(value_non_synth, viz, providers, start_indexes, matches)


class NatVisChildrenProvider(AbstractChildrenProvider):
    def __init__(self, value_non_synth: lldb.SBValue, viz: TypeViz, providers: list[AbstractChildrenProvider],
                 child_providers_start_indexes: list[int], wildcards: Sequence[str]):
        self.viz: TypeViz = viz
        self.child_providers: list[AbstractChildrenProvider] = providers
        self.child_providers_start_indexes: list[int] = child_providers_start_indexes
        self.format_spec: int = value_non_synth.GetFormat()
        self.wildcards = wildcards

    def num_children(self):
        return sum(child_prov.num_children() for child_prov in self.child_providers)

    # TODO: Remove this method in 2025.1
    def has_children(self):
        raise AssertionError("NatVisChildrenProvider.has_children is never called and should be removed")

    def get_child_index(self, name):
        if not self.child_providers:
            return INVALID_CHILD_INDEX

        for prov in self.child_providers:
            # noinspection PyBroadException
            try:
                index = prov.get_child_index(name)
            except Exception:
                # some unexpected error happened
                if not get_suppress_errors():
                    raise
                return INVALID_CHILD_INDEX

            if index != INVALID_CHILD_INDEX:
                return index

        return INVALID_CHILD_INDEX

    def get_child_at_index(self, index):
        if not self.child_providers:
            return None

        child_provider, relative_index = self._find_child_provider(index)
        if not child_provider:
            return None

        IntrinsicsPrologCache.update_current_intrinsics_scope(
            global_intrinsic_scope=self.viz.global_intrinsics,
            type_intrinsic_scope=self.viz.type_intrinsics,
            type_wildcards=self.wildcards)
        # noinspection PyBroadException
        try:
            child: lldb.SBValue = child_provider.get_child_at_index(relative_index)
            if child is not None:
                # apply inheritable formatting from parent value
                overlay_child_format(child, self.format_spec)
            return child
        except Exception:
            # some unexpected error happened
            if not get_suppress_errors():
                raise
            return None
        finally:
            IntrinsicsPrologCache.rollback_current_intrinsics_scope()

    def try_update_size(self, value_non_synth: lldb.SBValue) -> ChildrenProviderUpdateResult:
        old_size = self.num_children()
        change = ChildrenProviderUpdateResult.NONE
        for child_provider in self.child_providers:
            change |= child_provider.try_update_size(value_non_synth)
        if ChildrenProviderUpdateResult.SIZE_UPDATED in change:
            self.child_providers_start_indexes = _calculate_child_providers_start_indexes(self.child_providers)
        return ChildrenProviderUpdateResult.SIZE_UPDATED if old_size != self.num_children() else ChildrenProviderUpdateResult.NONE

    def _find_child_provider(self, index):
        # TODO: binary search, not linear
        for i, start_idx in enumerate(self.child_providers_start_indexes):
            if start_idx > index:
                # return previous provider
                prov_index = i - 1
                break
        else:
            # last provider
            prov_index = len(self.child_providers) - 1

        if prov_index == -1:
            return None, index

        prov = self.child_providers[prov_index]
        child_start_idx = self.child_providers_start_indexes[prov_index]

        return prov, (index - child_start_idx)


def _match_type_viz_template(type_viz_type_name_template: TypeNameTemplate,
                             type_name_template: TypeNameTemplate) -> Tuple[str, ...]:
    wildcard_matches: list[TypeNameTemplate] = []
    if not type_viz_type_name_template.match(type_name_template, wildcard_matches):
        raise Exception("Inconsistent type matching: can't match template {} with {}"
                        .format(type_name_template, type_viz_type_name_template))

    wildcard_matches_as_str = _fix_wildcard_matches(wildcard_matches)
    return tuple(wildcard_matches_as_str)


def optional_node_processor(fn):
    def wrapped(node, *args, **kwargs):
        assert isinstance(node, TypeVizItemOptionalNodeMixin)
        try:
            return fn(node, *args, **kwargs)
        except EvaluateError:
            if not node.optional:
                raise
        except Exception:
            raise
        return None

    return wrapped


def _evaluate_interpolated_string_to_stream(stream: Stream,
                                            interp_string: TypeVizInterpolatedString,
                                            ctx_val: lldb.SBValue,
                                            wildcards=None,
                                            context=None):
    max_stream_length = get_max_string_length()

    nested_stream = stream.create_nested()
    for (s, expr) in interp_string.parts_list:
        if nested_stream.length > max_stream_length:
            break
        nested_stream.output(s)
        if expr is not None:
            if nested_stream.length > max_stream_length:
                break
            _eval_display_string_expression(nested_stream, ctx_val, expr, wildcards, context)

    stream.output(str(nested_stream))
    return True


def _evaluate_interpolated_string(interp_string: TypeVizInterpolatedString, ctx_val, wildcards=None, context=None) -> str:
    target = ctx_val.GetTarget()
    is64bit: bool = target.GetAddressByteSize() == 8
    stream = Stream(is64bit, get_recursion_level())
    _evaluate_interpolated_string_to_stream(stream, interp_string, ctx_val, wildcards, context)
    return str(stream)


def _check_include_exclude_view_condition(viz: TypeViz, value_non_synth: lldb.SBValue) -> bool:
    if viz.include_view_id != 0:
        if get_custom_view_id(value_non_synth.GetFormat()) != viz.include_view_id:
            log("IncludeView condition is not satisfied '{}'...", str(viz.include_view))
            return False
    if viz.exclude_view_id != 0:
        if get_custom_view_id(value_non_synth.GetFormat()) == viz.exclude_view_id:
            log("ExcludeView condition is not satisfied '{}'...", str(viz.exclude_view))
            return False
    return True


@optional_node_processor
def _process_summary_node(summary: TypeVizSummary, ctx_val: lldb.SBValue, wildcards, stream: Stream):
    # ctx_val is NonSynthetic
    if summary.condition:
        if not _process_node_condition(summary.condition, ctx_val, wildcards):
            return None

    if not _evaluate_interpolated_string_to_stream(stream, summary.value, ctx_val, wildcards):
        return None
    return True


def _fix_wildcard_matches(matches: list[TypeNameTemplate]) -> list[str]:
    # remove breaking type prefixes from typenames
    def _remove_type_prefix(typename: str) -> str:
        prefix_list = ['struct ', 'class ']
        for prefix in prefix_list:
            if typename.startswith(prefix):
                typename = typename[len(prefix):]
        return typename

    return [_remove_type_prefix(str(t)) for t in matches]


def _try_create_child_providers(val_obj_non_synth: lldb.SBValue, viz: TypeViz, type_viz_name: TypeVizName,
                                type_name_template: TypeNameTemplate) -> Optional[list[AbstractChildrenProvider]]:
    log("Trying visualizer for type '{}'...", str(type_viz_name))
    wildcard_matches = _match_type_viz_template(type_viz_name.type_name_template, type_name_template)
    child_providers = _build_child_providers(viz.item_providers, val_obj_non_synth,
                                             wildcard_matches,
                                             viz.hide_raw_view) if viz.item_providers is not None else None
    return child_providers


def _calculate_child_providers_start_indexes(child_providers: list[AbstractChildrenProvider]) -> list[int]:
    start_idx = 0
    child_providers_start_indexes = []
    for prov in child_providers:
        child_providers_start_indexes.append(start_idx)
        start_idx += prov.num_children()

    return child_providers_start_indexes


def _check_condition(val: lldb.SBValue, condition: Optional[str], context: Optional[EvaluationContext] = None) -> bool:
    if not condition:
        # None or empty - means there is no condition
        return True
    res = eval_expression(val, '(bool)(' + condition + ')', context=context)
    if not res.GetValueAsUnsigned():
        return False
    return True


def _resolve_wildcards_in_interpolated_string(interp_string: TypeVizInterpolatedString, wildcards):
    parts_list = []
    for part in interp_string.parts_list:
        expr = part[1]
        if expr is None:
            parts_list.append((part[0], None))
            continue

        text = resolve_type_wildcards(expr.text, wildcards)
        options = expr.view_options
        array_size = resolve_type_wildcards(options.array_size, wildcards) if options.array_size else None
        format_spec = options.format_spec
        view_spec = options.view_spec
        expr = TypeVizExpression(text, array_size, format_spec, view_spec)
        parts_list.append((part[0], expr))

    return TypeVizInterpolatedString(parts_list)


def _convert_format_flags(format_flags: TypeVizFormatFlags) -> int:
    flags = 0
    for from_, to in TYPE_VIZ_FORMAT_FLAGS_TO_LLDB_FORMAT_MAP.items():
        if format_flags & from_:
            flags |= to
    return flags


def _apply_value_formatting_impl(val: lldb.SBValue, format_spec: TypeVizFormatSpec, format_flags: TypeVizFormatFlags, size: Optional[int],
                                 format_view_spec: int) -> lldb.SBValue:
    fmt = lldb.eFormatDefault
    # both format_spec and format_view_spec can't be set simultaneously
    if format_spec is not None:
        fmt = TYPE_VIZ_FORMAT_SPEC_TO_LLDB_FORMAT_MAP.get(format_spec, lldb.eFormatDefault)
    elif format_view_spec != 0:
        fmt = format_view_spec << 20

    if format_flags:
        fmt |= _convert_format_flags(format_flags)

    val_root = get_root_value(val)

    if size is not None:
        fmt |= eFormatAsArray
        val_root.SetFormatAsArraySize(size)

    # noinspection PyArgumentList
    val_root.SetFormat(fmt)

    if fmt & eFormatNoDerived:
        val.SetPreferDynamicValue(lldb.eNoDynamicValues)

    return val


def _apply_value_formatting(value_to_format: lldb.SBValue, format_options: TypeVizFormatOptions, context: lldb.SBValue,
                            wildcards: Sequence[str]) -> lldb.SBValue:
    array_size_expr = format_options.array_size
    if array_size_expr is not None and wildcards:
        array_size_expr = resolve_type_wildcards(array_size_expr, wildcards)
    size = _eval_expression_result_array_size(context, array_size_expr) if array_size_expr is not None else None
    value_to_format = _apply_value_formatting_impl(value_to_format, format_options.format_spec, format_options.format_flags, size,
                                                   format_options.view_spec_id)
    return value_to_format


def _eval_display_string_expression(stream: Stream, ctx: lldb.SBValue, expr: TypeVizExpression, wildcards: Sequence[str],
                                    context: Optional[EvaluationContext]) -> None:
    if stream.level >= g_max_recursion_level:
        return

    expr_text = resolve_type_wildcards(expr.text, wildcards) if wildcards else expr.text
    result = eval_expression(ctx, expr_text, context=context)
    result_non_synth = result.GetNonSyntheticValue()
    err = result_non_synth.GetError()
    if err.Fail():
        stream.output("???")
        return

    result = _apply_value_formatting(result, expr.view_options, ctx, wildcards)
    # parent value size formatting is not ignored only for summaries
    overlay_summary_format(result, ctx)

    stream.output_object(result_non_synth)


def _process_node_condition(condition: TypeVizCondition,
                            ctx_val: lldb.SBValue, wildcards, index: Optional[int] = None) -> bool:
    if condition.include_view_id != 0:
        if get_custom_view_id(ctx_val.GetFormat()) != condition.include_view_id:
            return False
    if condition.exclude_view_id != 0:
        if get_custom_view_id(ctx_val.GetFormat()) == condition.exclude_view_id:
            return False
    if condition.condition:
        processed_condition = resolve_type_wildcards(condition.condition, wildcards)
        if index is not None:
            processed_condition = processed_condition.replace('$i', str(index))

        if not _check_condition(ctx_val, processed_condition):
            return False
    return True


def _eval_expression_result_array_size(ctx, size_expr):
    size_value = eval_expression(ctx, size_expr)
    size = size_value.GetValueAsSigned()
    if not isinstance(size, int):
        raise EvaluateError('Size value must be of integer type')
    return size


@optional_node_processor
def _create_expanded_or_single_item_provider(item_provider: TypeVizItemProviderExpanded | TypeVizItemProviderSingle, ctx_val: lldb.SBValue,
                                             wildcards: Sequence[str]) -> ExpandedItemProvider | SingleItemProvider | None:
    if item_provider.condition:
        if not _process_node_condition(item_provider.condition, ctx_val, wildcards):
            return None

    expression = resolve_type_wildcards(item_provider.expr.text, wildcards)

    if isinstance(item_provider, TypeVizItemProviderExpanded):
        assert isinstance(item_provider, TypeVizItemSyntheticGetterNodeMixin)
        eval_settings = EvalSettings.with_metadata(None, item_provider.synthetic_getter)
        value = _evaluate_expression_and_apply_formatting(ctx_val, expression, eval_settings, item_provider.expr.view_options, wildcards)
        return ExpandedItemProvider(value)

    assert isinstance(item_provider, TypeVizItemProviderSingle)
    return SingleItemProvider(item_provider, ctx_val, expression, wildcards)


def _evaluate_expression_and_apply_formatting(ctx_val: lldb.SBValue, expression: str, eval_settings: EvalSettings,
                                              format_options: TypeVizFormatOptions, wildcards: Sequence[str]) -> lldb.SBValue:
    value = eval_expression(ctx_val, expression, eval_settings)
    value = _apply_value_formatting(value, format_options, ctx_val, wildcards)
    return value


class SingleItemProvider(AbstractChildrenProvider):
    def __init__(self, item_provider: TypeVizItemProviderSingle, value: lldb.SBValue, expression: str, wildcards: Sequence[str]):
        self.item_provider: TypeVizItemProviderSingle = item_provider
        self.value: lldb.SBValue = value
        self.expression: str = expression
        self.wildcards: Sequence[str] = wildcards

        # With the current design we need to precalculate value in constructor in order to fail fast with error if it happened.
        # Alternative (and possibly better) solution would be to return SBValue with error in `get_child_at_index`.
        self.precalculated_value = self._calculate_value()

    def num_children(self):
        return 1

    def get_child_index(self, name):
        if self.item_provider.name == name:
            return 0
        return INVALID_CHILD_INDEX

    def get_child_at_index(self, index):
        assert index == 0
        return self._calculate_value()

    def _calculate_value(self) -> lldb.SBValue:
        eval_settings = EvalSettings.with_metadata(self.item_provider.name, self.item_provider.synthetic_getter)
        return _evaluate_expression_and_apply_formatting(self.value, self.expression, eval_settings, self.item_provider.expr.view_options,
                                                         self.wildcards)


RAW_VIEW_ITEM_NAME = "Raw View"


class RawViewItemProvider(AbstractChildrenProvider):
    def __init__(self, value: lldb.SBValue):
        address = value.GetLoadAddress()
        child = value.CreateValueFromAddress(RAW_VIEW_ITEM_NAME, address, value.GetType())
        set_value_format(child, eFormatRawView)
        ItemExpression.copy_item_expression(value, child)
        self.value = child

    def num_children(self):
        return 1

    def get_child_index(self, name):
        if name == RAW_VIEW_ITEM_NAME:
            return 0
        return INVALID_CHILD_INDEX

    def get_child_at_index(self, index):
        assert index == 0
        return self.value


def _process_item_provider_single(item_provider: TypeVizItemProviderSingle, val: lldb.SBValue,
                                  wildcards: Sequence[str]) -> Optional[SingleItemProvider]:
    return _create_expanded_or_single_item_provider(item_provider, val, wildcards)


class ExpandedItemProvider(AbstractChildrenProvider):
    def __init__(self, cached_value: lldb.SBValue):
        self.expanded_value: lldb.SBValue = cached_value
        self.size: int = self.expanded_value.GetNumChildren()
        self.has_raw_view: bool = self.size != 0 and self.get_child_index(RAW_VIEW_ITEM_NAME) != INVALID_CHILD_INDEX

    def num_children(self) -> int:
        return self.size if not self.has_raw_view else self.size - 1

    def get_child_index(self, name: str) -> int:
        return self.expanded_value.GetIndexOfChildWithName(name)

    def get_child_at_index(self, index: int) -> lldb.SBValue:
        result: lldb.SBValue = self.expanded_value.GetChildAtIndex(index)
        update_value_dynamic_state(result)
        return result if result.GetNonSyntheticValue().GetName() != RAW_VIEW_ITEM_NAME else None

    def try_update_size(self, value_non_synth: lldb.SBValue) -> ChildrenProviderUpdateResult:
        old_size = self.size
        self.size = self.expanded_value.GetNumChildren()
        return ChildrenProviderUpdateResult.SIZE_UPDATED if old_size != self.size else ChildrenProviderUpdateResult.NONE

def _process_item_provider_expanded(item_provider: TypeVizItemProviderExpanded, val: lldb.SBValue,
                                    wildcards: Sequence[str]) -> Optional[ExpandedItemProvider]:
    return _create_expanded_or_single_item_provider(item_provider, val, wildcards)


def _find_first_good_node(node_proc: Callable, nodes: list[Any], *args, **kwargs) -> Optional[Any]:
    return _find_first_good_node_and_index(node_proc, nodes, *args, **kwargs)[0]


def _find_first_good_node_and_index(node_proc: Callable, nodes: list[Any], *args, **kwargs) -> Tuple[Optional[Any], int]:
    for (index, node) in enumerate(nodes):
        item_value = node_proc(node, *args, **kwargs)
        if item_value is not None:
            return item_value, index
    return None, -1


@optional_node_processor
def _node_processor_size(size_node: TypeVizItemSizeTypeNode, ctx_val: lldb.SBValue, wildcards: Sequence[str]) -> Optional[int]:
    assert isinstance(size_node, TypeVizItemSizeTypeNode)
    if size_node.condition:
        if not _process_node_condition(size_node.condition, ctx_val, wildcards):
            return None

    expression = size_node.text
    expression = resolve_type_wildcards(expression, wildcards)
    value = eval_expression(ctx_val, expression)
    result_value = value.GetValueAsSigned()
    if not isinstance(result_value, int):
        raise EvaluateError('Size value must be of integer type')

    return result_value


def _node_processor_array_items_value_pointer(value_pointer_node: TypeVizItemValuePointerTypeNode, ctx_val: lldb.SBValue,
                                              wildcards: Sequence[str]) -> Optional[lldb.SBValue]:
    assert isinstance(value_pointer_node, TypeVizItemValuePointerTypeNode)
    if value_pointer_node.condition:
        if not _process_node_condition(value_pointer_node.condition, ctx_val, wildcards):
            return None

    expr = value_pointer_node.expr
    expression = resolve_type_wildcards(expr.text, wildcards)
    eval_settings = EvalSettings.with_metadata()
    return _evaluate_expression_and_apply_formatting(ctx_val, expression, eval_settings, expr.view_options, wildcards)


class ArrayItemsProvider(AbstractChildrenProvider):
    def __init__(self, items_provider: TypeVizItemProviderArrayItems, size: int, value_pointer: lldb.SBValue, elem_type: lldb.SBType,
                 wildcards: Sequence[str], element_getter: Optional[SyntheticMethod]):
        self.items_provider: TypeVizItemProviderArrayItems = items_provider
        self.size: int = size
        self.value_pointer: lldb.SBValue = value_pointer
        self.elem_type: lldb.SBType = elem_type
        self.elem_byte_size: int = elem_type.GetByteSize()
        self.wildcards: Sequence[str] = wildcards
        self.element_getter: Optional[SyntheticMethod] = element_getter

    def num_children(self):
        return self.size

    def get_child_index(self, name):
        try:
            return int(name.lstrip('[').rstrip(']'))
        except ValueError:
            return INVALID_CHILD_INDEX

    def get_child_at_index(self, index):
        child_name = "[{}]".format(index)
        offset = index * self.elem_byte_size
        child = self.value_pointer.CreateChildAtOffset(child_name, offset, self.elem_type)
        getter_call = self.element_getter.method_call([str(index)]) if self.element_getter is not None else None
        ItemExpression.update_item_expression(child, self.value_pointer, child_name, getter_call)
        return child

    def try_update_size(self, value_non_synth: lldb.SBValue) -> ChildrenProviderUpdateResult:
        new_provider = _create_array_items_provider(self.items_provider, value_non_synth, self.wildcards)
        if new_provider is None:
            # That probably means that this provider is no longer valid, and we should rebuild all providers. But that should be rare case.
            return ChildrenProviderUpdateResult.NONE

        old_size = self.size
        self.size = new_provider.size
        self.value_pointer = new_provider.value_pointer
        assert self.elem_type == new_provider.elem_type
        self.elem_type = new_provider.elem_type
        self.elem_byte_size = new_provider.elem_type.GetByteSize()
        self.element_getter = new_provider.element_getter
        return ChildrenProviderUpdateResult.SIZE_UPDATED if old_size != self.size else ChildrenProviderUpdateResult.NONE


@optional_node_processor
def _create_array_items_provider(items_provider: TypeVizItemProviderArrayItems, ctx_val: lldb.SBValue,
                                 wildcards: Sequence[str]) -> Optional[ArrayItemsProvider]:
    assert isinstance(items_provider, TypeVizItemProviderArrayItems)
    if items_provider.condition:
        if not _process_node_condition(items_provider.condition, ctx_val, wildcards):
            return None

    size = _calculate_items_provider_size(items_provider.size_nodes, ctx_val, wildcards)
    if size is None:
        raise EvaluateError('No valid Size node found')

    (value_pointer_value, index) = _find_first_good_node_and_index(_node_processor_array_items_value_pointer,
                                                                   items_provider.value_pointer_nodes,
                                                                   ctx_val, wildcards)
    if value_pointer_value is None:
        raise EvaluateError('No valid ValuePointerType node found')

    value_pointer_type = value_pointer_value.GetNonSyntheticValue().GetType()
    if value_pointer_type.IsPointerType():
        elem_type = value_pointer_type.GetPointeeType()
    elif value_pointer_type.IsArrayType():
        elem_type = value_pointer_type.GetArrayElementType()
        value_pointer_value = ItemExpression.array_address_of(value_pointer_value)
    else:
        raise EvaluateError('Value pointer is not of pointer or array type ({})'.format(str(value_pointer_type)))

    element_getter = items_provider.value_pointer_nodes[index].synthetic_getter or items_provider.synthetic_getter
    if element_getter is not None:
        ItemExpression.copy_item_expression(ctx_val, value_pointer_value)
    return ArrayItemsProvider(items_provider, size, value_pointer_value, elem_type, wildcards, element_getter)


def _process_item_provider_array_items(item_provider, val, wildcards):
    return _create_array_items_provider(item_provider, val, wildcards)


LIST_ITEM_SUBSCRIPT_INDEX_REGEX = re.compile(r'\s*\[\s*\$i\s*]\s*')

g_cache_subscript_is_missing = dict[str, dict[str, str]]()


def _trying_eval_list_item_indexed_value(expression: str, ctx_val: lldb.SBValue, idx: int, name: str,
                                         element_getter: Optional[SyntheticMethod]) -> Tuple[Optional[lldb.SBValue], Optional[str]]:
    try:
        expanded_expression = expression.replace('$i', str(idx))
        eval_settings = EvalSettings.with_metadata(name, element_getter, [str(idx)])
        value: lldb.SBValue = eval_expression(ctx_val, expanded_expression, eval_settings)
        return value, None
    except EvaluateError as evaluate_error:
        type_doesnt_have_subscript = evaluate_error.args and \
                                     evaluate_error.args[0] and \
                                     evaluate_error.args[0].endswith('does not provide a subscript operator')
        if not type_doesnt_have_subscript:
            raise

    log("Subscript operator is missing. Wa are trying to get children via natvis. "
        "At the moment, we only support the simple syntax 'member[$i]'")

    res = LIST_ITEM_SUBSCRIPT_INDEX_REGEX.search(expression)
    if res is None:
        log("We can't find expression '[$i]' for evaluate in '{}'", expression)
        raise EvaluateError("We can't find expression '[$i]' for evaluate")

    another_occurrence = LIST_ITEM_SUBSCRIPT_INDEX_REGEX.search(expression, pos=res.end())
    if another_occurrence is not None:
        log("There are more than one expression '[$i]' in '{}'", expression)
        raise EvaluateError("More than one expression '[$i]'")

    child_item_expression = expression[:res.start()]
    return None, child_item_expression


def _trying_get_indexed_item_value(expression: str, ctx_val: lldb.SBValue, idx: int,
                                   name: str, element_getter: Optional[SyntheticMethod]) -> lldb.SBValue:
    type_name = ctx_val.type.name
    if (dict_with_expr := g_cache_subscript_is_missing.get(type_name, None)) is None:
        g_cache_subscript_is_missing[type_name] = dict[str, str]()
    child_item_expression = dict_with_expr.get(expression, None) if dict_with_expr else None

    already_calculated = child_item_expression is not None

    if not already_calculated:
        value, child_item_expression = _trying_eval_list_item_indexed_value(expression, ctx_val, idx, name,
                                                                            element_getter)
        if value:
            return value

    value: lldb.SBValue = eval_expression(ctx_val, child_item_expression, EvalSettings.with_metadata(name))

    if not already_calculated:
        child_item_type_name = value.type.name
        # noinspection PyUnusedLocal
        force_evaluate_children_count = value.num_children

        if child_item_type_name not in IndexListItemsProvider.types_with_index_list_items:
            log("IndexListItems expression '{}' doesn't have subscript operator "
                "and doesn't have implementation in natvis type '{}'", expression, child_item_type_name)
            raise EvaluateError("IndexListItems item doesn't have subscript operator")

        g_cache_subscript_is_missing[type_name][expression] = child_item_expression

    return value.GetChildAtIndex(idx)


def _node_processor_index_list_items_value_node(idx: int, name: str,
                                                index_list_value_node: TypeVizItemIndexNodeTypeNode,
                                                ctx_val: lldb.SBValue, wildcards: Sequence[str],
                                                element_getter: Optional[SyntheticMethod]) -> Optional[lldb.SBValue]:
    if index_list_value_node.condition:
        if not _process_node_condition(index_list_value_node.condition, ctx_val, wildcards, idx):
            return None

    expression = index_list_value_node.expr.text
    expression = resolve_type_wildcards(expression, wildcards)
    value = _trying_get_indexed_item_value(expression, ctx_val, idx, name, element_getter)
    return _apply_value_formatting(value, index_list_value_node.expr.view_options, ctx_val, wildcards)


class IndexListItemsProvider(AbstractChildrenProvider):
    types_with_index_list_items: set[str] = set[str]()

    def __init__(self, size: int, items_provider: TypeVizItemProviderIndexListItems, ctx_val: lldb.SBValue, wildcards: Sequence[str]):
        self.size: int = size
        self.items_provider: TypeVizItemProviderIndexListItems = items_provider
        self.ctx_val: lldb.SBValue = ctx_val
        self.wildcards: Sequence[str] = wildcards

        IndexListItemsProvider.types_with_index_list_items.add(ctx_val.type.name)

    def num_children(self):
        return self.size

    def get_child_index(self, name):
        try:
            return int(name.lstrip('[').rstrip(']'))
        except ValueError:
            return INVALID_CHILD_INDEX

    def get_child_at_index(self, index):
        name = "[{}]".format(index)
        value = None
        for value_node_node in self.items_provider.value_node_nodes:
            element_getter = value_node_node.synthetic_getter or self.items_provider.synthetic_getter
            value = _node_processor_index_list_items_value_node(index, name, value_node_node, self.ctx_val, self.wildcards, element_getter)
            if value:
                break

        # TODO: show some error value on None
        return value

    def try_update_size(self, value_non_synth: lldb.SBValue) -> ChildrenProviderUpdateResult:
        old_size = self.size
        self.size = _calculate_items_provider_size(self.items_provider.size_nodes, self.ctx_val, self.wildcards)
        return ChildrenProviderUpdateResult.SIZE_UPDATED if old_size != self.size else ChildrenProviderUpdateResult.NONE


@optional_node_processor
def _create_index_list_items_provider(items_provider: TypeVizItemProviderIndexListItems, ctx_val: lldb.SBValue,
                                      wildcards: Sequence[str]) -> Optional[IndexListItemsProvider]:
    assert isinstance(items_provider, TypeVizItemProviderIndexListItems)
    if items_provider.condition:
        if not _process_node_condition(items_provider.condition, ctx_val, wildcards):
            return None

    size = _calculate_items_provider_size(items_provider.size_nodes, ctx_val, wildcards)
    if size is None:
        raise EvaluateError('No valid Size node found')

    return IndexListItemsProvider(size, items_provider, ctx_val, wildcards)


def _process_item_provider_index_list_items(item_provider, val, wildcards):
    return _create_index_list_items_provider(item_provider, val, wildcards)


def _is_valid_node_ptr(node):
    if node is None:
        return False

    if not node.TypeIsPointerType():
        return False

    return True


def _get_ptr_value(node):
    val = node.GetNonSyntheticValue()
    return val.GetValueAsUnsigned() if _is_valid_node_ptr(val) else 0


class NodesProvider(object):
    def __init__(self, ctx_val: lldb.SBValue):
        self._ctx_val: lldb.SBValue = ctx_val
        self._next_node_index: int = 0
        self.cache: list[Optional[lldb.SBValue]] = []
        self.cache_size: int = 0
        self.has_more: bool = False
        self.names: Optional[list[str]] = None
        self.name2index: Optional[dict[str, int]] = None

    def ensure_node_calculated(self, index: int) -> None:
        cached_node = self.cache[index]
        if cached_node is None:
            self._calculate_cached_nodes(index)
            cached_node = self.cache[index]
            ItemExpression.copy_item_expression(self._ctx_val, cached_node)

    def update_cache_for_synthetic_getter(self, this_ctx: lldb.SBValue,
                                          type_viz_node: TypeVizItemSyntheticGetterNodeMixin) -> None:
        if type_viz_node.synthetic_getter is not None:
            for cached_node in self.cache:
                if cached_node is not None:
                    ItemExpression.copy_item_expression(this_ctx, cached_node)

    def _prepare_cache(self, known_size: Optional[int]) -> None:
        if known_size is not None:
            # Create empty cache. It will be calculated lazily
            self.cache = [None] * known_size
            self.has_more = False
            self.cache_size = len(self.cache)
            return

        self.cache = []
        self._calculate_cached_nodes(g_max_num_children)
        self._has_more = self._has_non_calculated_nodes() and self._next_node_index > g_max_num_children
        self.cache_size = len(self.cache)

    def _set_calculated_node(self, next_value: lldb.SBValue) -> None:
        self._process_node_name(next_value, self._next_node_index)
        if self._next_node_index < len(self.cache):
            assert self.cache[self._next_node_index] is None
            self.cache[self._next_node_index] = next_value
        else:
            assert self._next_node_index == len(self.cache)
            self.cache.append(next_value)
        self._next_node_index += 1

    def _process_node_name(self, node: lldb.SBValue, index: int) -> None:
        pass

    def _calculate_cached_nodes(self, stop_at: int) -> None:
        raise NotImplementedError

    def _has_non_calculated_nodes(self) -> bool:
        raise NotImplementedError


class CustomItemsProvider(AbstractChildrenProvider):
    def __init__(self, items_provider: TypeVizItemProviderTreeItems | TypeVizItemProviderLinkedListItems, nodes_provider: NodesProvider,
                 value_expression: str, value_opts: TypeVizFormatOptions, wildcards: Sequence[str],
                 element_getter: Optional[SyntheticMethod]):
        assert isinstance(nodes_provider, NodesProvider)

        self.items_provider: TypeVizItemProviderTreeItems | TypeVizItemProviderLinkedListItems = items_provider
        self.nodes_provider: NodesProvider = nodes_provider
        self.value_expression: str = value_expression
        self.value_opts: TypeVizFormatOptions = value_opts
        self.wildcards: Sequence[str] = wildcards
        self.element_getter: Optional[SyntheticMethod] = element_getter

    def num_children(self) -> int:
        return self.nodes_provider.cache_size

    def get_child_index(self, name: str) -> int:
        if self.nodes_provider.name2index:
            return self.nodes_provider.name2index.get(name, INVALID_CHILD_INDEX)

        try:
            return int(name.lstrip('[').rstrip(']'))
        except ValueError:
            return INVALID_CHILD_INDEX

    def get_child_at_index(self, index: int) -> lldb.SBValue:
        if index < 0 or index >= self.nodes_provider.cache_size:
            raise IndexError(f"Index {index} is out of range [0; {self.nodes_provider.cache_size})")

        self.nodes_provider.ensure_node_calculated(index)
        node_value: Optional[lldb.SBValue] = self.nodes_provider.cache[index]
        if node_value is None:
            raise EvaluateError(f"Node {index} was not evaluated")

        if self.nodes_provider.names:
            name = self.nodes_provider.names[index]
        else:
            name = "[{}]".format(index)
        eval_settings = EvalSettings.with_metadata(name, self.element_getter, [str(index)])
        return _evaluate_expression_and_apply_formatting(node_value, self.value_expression, eval_settings, self.value_opts, self.wildcards)

    def try_update_size(self, value_non_synth: lldb.SBValue) -> ChildrenProviderUpdateResult:
        old_size = self.nodes_provider.cache_size
        new_size = _calculate_items_provider_size(self.items_provider.size_nodes, value_non_synth, self.wildcards)
        self.nodes_provider = _create_nodes_provider(self.items_provider, value_non_synth, self.wildcards, new_size)
        return ChildrenProviderUpdateResult.SIZE_UPDATED if old_size != new_size else ChildrenProviderUpdateResult.NONE


class LinkedListIterator(object):
    def __init__(self, node_value, next_expression):
        self.node_value = node_value
        self.next_expression = next_expression

    def __bool__(self):
        return _get_ptr_value(self.node_value) != 0

    def __eq__(self, other):
        return _get_ptr_value(self.node_value) == _get_ptr_value(other.node_value)

    def cur_value(self):
        return ItemExpression.dereference(self.node_value.GetNonSyntheticValue())

    def cur_ptr(self):
        return self.node_value.GetNonSyntheticValue().GetValueAsUnsigned()

    def move_to_next(self):
        self.node_value = self._next()

    def _next(self):
        return eval_expression(self.cur_value(), self.next_expression, EvalSettings.with_metadata())


class LinkedListNodesProvider(NodesProvider):
    def __init__(self, ctx_val: lldb.SBValue, head_pointer: lldb.SBValue, next_expression: str):
        super().__init__(ctx_val)
        self._iterator = LinkedListIterator(head_pointer, next_expression)
        self._head_node_value = _get_ptr_value(self._iterator.node_value)

    def _calculate_cached_nodes(self, stop_at: int) -> None:
        # iterate list nodes and cache them
        while self._has_non_calculated_nodes() and self._next_node_index <= stop_at:
            next_value = self._iterator.cur_value()
            self._set_calculated_node(next_value)
            self._iterator.move_to_next()
            if self._iterator and _get_ptr_value(self._iterator.node_value) == self._head_node_value:
                # TODO: This loop detection is not entirely correct
                break

    def _has_non_calculated_nodes(self) -> bool:
        return bool(self._iterator)


class LinkedListIndexedNodesProvider(LinkedListNodesProvider):
    def __init__(self, ctx_val: lldb.SBValue, size: Optional[int], head_pointer: lldb.SBValue, next_expression: str):
        super().__init__(ctx_val, head_pointer, next_expression)
        self._prepare_cache(size)


class LinkedListCustomNameNodesProvider(LinkedListNodesProvider):
    def __init__(self, ctx_val: lldb.SBValue, size: Optional[int], head_pointer: lldb.SBValue, next_expression: str,
                 custom_value_name: TypeVizInterpolatedString, wildcards: Sequence[str]):
        super().__init__(ctx_val, head_pointer, next_expression)
        self._custom_value_name = custom_value_name
        self._wildcards = wildcards
        self.names = []
        self.name2index = {}
        self._prepare_cache(size)

    def _process_node_name(self, node: lldb.SBValue, index: int) -> None:
        name = _evaluate_interpolated_string(self._custom_value_name, node, self._wildcards)
        self.names.append(name)
        self.name2index[name] = index


def _node_processor_linked_list_items_head_pointer(head_pointer_node: TypeVizItemListItemsHeadPointerTypeNode, ctx_val: lldb.SBValue,
                                                   wildcards: Sequence[str]) -> lldb.SBValue:
    assert isinstance(head_pointer_node, TypeVizItemListItemsHeadPointerTypeNode)
    expression = resolve_type_wildcards(head_pointer_node.text, wildcards)
    return eval_expression(ctx_val, expression, EvalSettings.with_metadata())


def _calculate_items_provider_size(size_nodes: List[TypeVizItemSizeTypeNode], ctx_val: lldb.SBValue,
                                   wildcards: Sequence[str]) -> Optional[int]:
    return _find_first_good_node(_node_processor_size, size_nodes, ctx_val, wildcards)


def _create_linked_list_nodes_provider(items_provider: TypeVizItemProviderLinkedListItems, ctx_val: lldb.SBValue, wildcards: Sequence[str],
                                       size: Optional[int]) -> NodesProvider:
    next_pointer_node = items_provider.next_pointer_node
    assert isinstance(next_pointer_node, TypeVizItemListItemsNextPointerTypeNode)

    value_node = items_provider.value_node_node
    assert isinstance(value_node, TypeVizItemListItemsIndexNodeTypeNode)

    head_pointer_value = _node_processor_linked_list_items_head_pointer(items_provider.head_pointer_node, ctx_val, wildcards)
    next_pointer_expression = resolve_type_wildcards(next_pointer_node.text, wildcards)
    if value_node.name is None:
        nodes_provider = LinkedListIndexedNodesProvider(ctx_val, size, head_pointer_value, next_pointer_expression)
    else:
        nodes_provider = LinkedListCustomNameNodesProvider(ctx_val, size, head_pointer_value, next_pointer_expression, value_node.name,
                                                           wildcards)
    return nodes_provider


def _create_nodes_provider(items_provider: TypeVizItemProviderLinkedListItems | TypeVizItemProviderTreeItems, ctx_val: lldb.SBValue,
                           wildcards: Sequence[str], size: Optional[int]) -> NodesProvider:
    if not isinstance(items_provider, TypeVizItemProviderLinkedListItems) and not isinstance(items_provider, TypeVizItemProviderTreeItems):
        raise TypeError(f"TypeVizItemProviderLinkedListItems or TypeVizItemProviderTreeItems are expected; got {type(items_provider)}")
    nodes_provider = _create_linked_list_nodes_provider(items_provider, ctx_val, wildcards, size) \
        if isinstance(items_provider, TypeVizItemProviderLinkedListItems) \
        else _create_binary_tree_nodes_provider(items_provider, ctx_val, wildcards, size)
    nodes_provider.update_cache_for_synthetic_getter(ctx_val, items_provider)
    return nodes_provider


@optional_node_processor
def _create_custom_items_provider(items_provider: TypeVizItemProviderLinkedListItems | TypeVizItemProviderTreeItems, ctx_val: lldb.SBValue,
                                  wildcards: Sequence[str]) -> Optional[CustomItemsProvider]:
    if items_provider.condition:
        if not _process_node_condition(items_provider.condition, ctx_val, wildcards):
            return None

    size = _calculate_items_provider_size(items_provider.size_nodes, ctx_val, wildcards)
    nodes_provider = _create_nodes_provider(items_provider, ctx_val, wildcards, size)
    value_node = items_provider.value_node_node
    value_expression = resolve_type_wildcards(value_node.expr.text, wildcards)
    value_opts = value_node.expr.view_options
    return CustomItemsProvider(items_provider, nodes_provider, value_expression, value_opts, wildcards, items_provider.synthetic_getter)


def _process_item_provider_linked_list_items(items_provider: TypeVizItemProviderLinkedListItems, val: lldb.SBValue,
                                             wildcards: Sequence[str]) -> Optional[CustomItemsProvider]:
    return _create_custom_items_provider(items_provider, val, wildcards)


class BinaryTreeNodesProvider(NodesProvider):
    def __init__(self, ctx_val: lldb.SBValue, head_pointer: lldb.SBValue, left_expression: str, right_expression: str,
                 node_condition: Optional[str]):
        super().__init__(ctx_val)
        self._next_node_pointer: lldb.SBValue = head_pointer
        self._parent_nodes_stack: List[lldb.SBValue] = []
        self._left_expression: str = left_expression
        self._right_expression: str = right_expression
        self._node_condition: Optional[str] = node_condition

    def _calculate_cached_nodes(self, stop_at: int) -> None:
        # iterate list nodes and cache them
        while self._has_non_calculated_nodes() and self._next_node_index <= stop_at:
            while _get_ptr_value(self._next_node_pointer) != 0 and self._check_node_condition(self._next_node_pointer):
                if len(self._parent_nodes_stack) > 100:  # ~2^100 nodes can't be true - something went wrong
                    raise Exception("Invalid tree")

                self._parent_nodes_stack.append(self._next_node_pointer)
                next_dereferenced = ItemExpression.dereference(self._next_node_pointer.GetNonSyntheticValue())
                self._next_node_pointer = eval_expression(next_dereferenced, self._left_expression, EvalSettings.with_metadata())

            self._next_node_pointer = self._parent_nodes_stack.pop()
            next_dereferenced = ItemExpression.dereference(self._next_node_pointer.GetNonSyntheticValue())
            self._set_calculated_node(next_dereferenced)
            self._next_node_pointer = eval_expression(next_dereferenced, self._right_expression, EvalSettings.with_metadata())

    def _has_non_calculated_nodes(self) -> bool:
        return (_get_ptr_value(self._next_node_pointer) != 0 and self._check_node_condition(self._next_node_pointer) or
                self._parent_nodes_stack)

    def _check_node_condition(self, node: lldb.SBValue) -> bool:
        return _check_condition(node.GetNonSyntheticValue().Dereference(), self._node_condition)


class BinaryTreeIndexedNodesProvider(BinaryTreeNodesProvider):
    def __init__(self, ctx_val: lldb.SBValue, size: Optional[int], head_pointer: lldb.SBValue, left_expression: str, right_expression: str,
                 node_condition: Optional[str]):
        super().__init__(ctx_val, head_pointer, left_expression, right_expression, node_condition)
        self._prepare_cache(size)


class BinaryTreeCustomNamesNodesProvider(BinaryTreeNodesProvider):
    def __init__(self, ctx_val: lldb.SBValue, size: Optional[int], head_pointer: lldb.SBValue, left_expression: str, right_expression: str,
                 node_condition: Optional[str], custom_value_name: TypeVizInterpolatedString, wildcards: Sequence[str]):
        super().__init__(ctx_val, head_pointer, left_expression, right_expression, node_condition)
        self._custom_value_name = custom_value_name
        self._wildcards = wildcards
        self.names = []
        self.name2index = {}
        self._prepare_cache(size)

    def _process_node_name(self, node: lldb.SBValue, index: int) -> None:
        name = _evaluate_interpolated_string(self._custom_value_name, node, self._wildcards)
        self.names.append(name)
        self.name2index[name] = index


def _node_processor_tree_items_head_pointer(head_pointer_node: TypeVizItemTreeHeadPointerTypeNode, ctx_val: lldb.SBValue,
                                            wildcards: Sequence[str]) -> lldb.SBValue:
    assert isinstance(head_pointer_node, TypeVizItemTreeHeadPointerTypeNode)
    expression = resolve_type_wildcards(head_pointer_node.text, wildcards)
    return eval_expression(ctx_val, expression, EvalSettings.with_metadata())


def _create_binary_tree_nodes_provider(items_provider: TypeVizItemProviderTreeItems, ctx_val: lldb.SBValue, wildcards: Sequence[str],
                                       size: Optional[int]) -> NodesProvider:
    left_pointer_node = items_provider.left_pointer_node
    assert isinstance(left_pointer_node, TypeVizItemTreeChildPointerTypeNode)

    right_pointer_node = items_provider.right_pointer_node
    assert isinstance(right_pointer_node, TypeVizItemTreeChildPointerTypeNode)

    value_node = items_provider.value_node_node
    assert isinstance(value_node, TypeVizItemTreeNodeTypeNode)

    head_pointer_value = _node_processor_tree_items_head_pointer(items_provider.head_pointer_node, ctx_val, wildcards)
    left_pointer_expression = resolve_type_wildcards(left_pointer_node.text, wildcards)
    right_pointer_expression = resolve_type_wildcards(right_pointer_node.text, wildcards)
    condition = value_node.condition
    value_condition = resolve_type_wildcards(condition.condition, wildcards) if condition and condition.condition else None
    if value_node.name is None:
        nodes_provider = BinaryTreeIndexedNodesProvider(ctx_val, size, head_pointer_value, left_pointer_expression,
                                                        right_pointer_expression, value_condition)
    else:
        nodes_provider = BinaryTreeCustomNamesNodesProvider(ctx_val, size, head_pointer_value, left_pointer_expression,
                                                            right_pointer_expression, value_condition, value_node.name, wildcards)
    return nodes_provider


def _process_item_provider_tree_items(items_provider: TypeVizItemProviderTreeItems, val: lldb.SBValue,
                                      wildcards: Sequence[str]) -> Optional[CustomItemsProvider]:
    return _create_custom_items_provider(items_provider, val, wildcards)


class CustomListItemsInstruction(object):
    def __init__(self, next_instruction: Optional[CustomListItemsInstruction], condition: Optional[str]):
        self.next_instruction: Optional[CustomListItemsInstruction] = next_instruction
        self.condition: Optional[str] = condition

    def evaluate_condition(self, ctx_val: lldb.SBValue, context: EvaluationContext) -> bool:
        return _check_condition(ctx_val, self.condition, context)

    def execute(self, ctx_val: lldb.SBValue, context: EvaluationContext,
                items_collector: List[lldb.SBValue]) -> Optional[CustomListItemsInstruction]:
        raise NotImplementedError


class CustomListItemsExecInstruction(CustomListItemsInstruction):
    def __init__(self, code, condition, next_instruction: Optional[CustomListItemsInstruction]):
        super(CustomListItemsExecInstruction, self).__init__(next_instruction, condition)
        self.code = code

    def execute(self, ctx_val: lldb.SBValue, context: EvaluationContext, items_collector: List[lldb.SBValue]):
        if self.evaluate_condition(ctx_val, context):
            eval_expression(ctx_val, self.code, context=context)
        return self.next_instruction


class CustomListItemsItemInstruction(CustomListItemsInstruction):
    def __init__(self, name: TypeVizInterpolatedString, expr, opts, condition, next_instruction: Optional[CustomListItemsInstruction]):
        super(CustomListItemsItemInstruction, self).__init__(next_instruction, condition)
        self.name: TypeVizInterpolatedString = name
        self.expr = expr
        self.opts = opts

    def execute(self, ctx_val: lldb.SBValue, context: EvaluationContext, items_collector: List[lldb.SBValue]):
        if self.evaluate_condition(ctx_val, context):
            if self.name:
                name = _evaluate_interpolated_string(self.name, ctx_val, context=context)
            else:
                name = "[{}]".format(len(items_collector))

            item = eval_expression(ctx_val, self.expr, EvalSettings.with_metadata(name), context)
            if self.opts.array_size:
                size_value = eval_expression(ctx_val, self.opts.array_size, context=context)
                size = size_value.GetValueAsSigned()
            else:
                size = None

            item = _apply_value_formatting_impl(item, self.opts.format_spec, self.opts.format_flags, size, self.opts.view_spec_id)
            items_collector.append(item)
            return self.next_instruction
        return self.next_instruction


class CustomListItemsIfInstruction(CustomListItemsInstruction):
    def __init__(self, condition, then_instruction, next_instruction: Optional[CustomListItemsInstruction]):
        super(CustomListItemsIfInstruction, self).__init__(next_instruction, condition)
        self.then_instruction = then_instruction

    def execute(self, ctx_val: lldb.SBValue, context: EvaluationContext, items_collector: List[lldb.SBValue]):
        if self.evaluate_condition(ctx_val, context):
            return self.then_instruction
        return self.next_instruction


def _process_code_block_nodes(block_nodes: List, wildcards: Sequence[str], next_instr: Optional[CustomListItemsInstruction],
                              loop_breaks: List[CustomListItemsInstruction]) -> Optional[CustomListItemsInstruction]:
    end_if_instr = None
    for node in reversed(block_nodes):
        if isinstance(node, TypeVizItemExecCodeBlockTypeNode):
            value = resolve_type_wildcards(node.value, wildcards)
            condition = resolve_type_wildcards(node.condition, wildcards) if node.condition else None
            next_instr = CustomListItemsExecInstruction(value, condition, next_instr)
        elif isinstance(node, TypeVizItemItemCodeBlockTypeNode):
            name = _resolve_wildcards_in_interpolated_string(node.name, wildcards) if node.name else None
            expression = resolve_type_wildcards(node.expr.text, wildcards)
            opts = node.expr.view_options
            condition = resolve_type_wildcards(node.condition, wildcards) if node.condition else None
            next_instr = CustomListItemsItemInstruction(name, expression, opts, condition, next_instr)
        elif isinstance(node, TypeVizItemIfCodeBlockTypeNode):
            condition = resolve_type_wildcards(node.condition, wildcards)
            if not end_if_instr:
                end_if_instr = next_instr
            then_instr = _process_code_block_nodes(node.code_blocks, wildcards, end_if_instr, loop_breaks)
            next_instr = CustomListItemsIfInstruction(condition, then_instr, next_instr)
            end_if_instr = None
        elif isinstance(node, TypeVizItemElseCodeBlockTypeNode):
            end_if_instr = next_instr
            next_instr = _process_code_block_nodes(node.code_blocks, wildcards, next_instr, loop_breaks)
        elif isinstance(node, TypeVizItemElseIfCodeBlockTypeNode):
            condition = resolve_type_wildcards(node.condition, wildcards) if node.condition else None
            if not end_if_instr:
                end_if_instr = next_instr
            then_instr = _process_code_block_nodes(node.code_blocks, wildcards, end_if_instr, loop_breaks)
            next_instr = CustomListItemsIfInstruction(condition, then_instr, next_instr)
        elif isinstance(node, TypeVizItemLoopCodeBlockTypeNode):
            condition = resolve_type_wildcards(node.condition, wildcards) if node.condition else None
            loop_instr = CustomListItemsIfInstruction(condition, None, next_instr)
            loop_breaks.append(next_instr)
            then_instr = _process_code_block_nodes(node.code_blocks, wildcards, loop_instr, loop_breaks)
            loop_breaks.pop()
            loop_instr.then_instruction = then_instr
            next_instr = loop_instr
        elif isinstance(node, TypeVizItemBreakCodeBlockTypeNode):
            if node.condition and node.condition != "":
                condition = resolve_type_wildcards(node.condition, wildcards)
                next_instr = CustomListItemsIfInstruction(condition, loop_breaks[-1], next_instr)
            else:
                next_instr = loop_breaks[-1]

    return next_instr


g_static_counter = 0


def _process_variables_nodes(variable_nodes: List[TypeVizItemVariableTypeNode], wildcards):
    prolog_collection = []
    epilog_collection = []
    first_time_code_collection = []
    code_collection = []
    for node in variable_nodes:
        initial_value = resolve_type_wildcards(node.initial_value, wildcards)

        global g_static_counter
        g_static_counter += 1
        persistent_name = "$" + node.name + str(g_static_counter)
        first_time_code_collection.append("auto {} = {};".format(persistent_name, initial_value))
        code_collection.append("{} = {};".format(persistent_name, initial_value))

        prolog_collection.append("auto {} = {};".format(node.name, persistent_name))
        epilog_collection.append("{} = {};".format(persistent_name, node.name))
    prolog = "".join(prolog_collection)
    epilog = "".join(epilog_collection)
    code = "".join(code_collection) + "1"
    first_time_code = "".join(first_time_code_collection) + "1"

    def create_context(ctx_var: lldb.SBValue, first_time: bool):
        options = lldb.SBExpressionOptions()
        eval_expression(ctx_var, first_time_code if first_time else code, settings=EvalSettings(options=options))

        return EvaluationContext(prolog, epilog)

    return create_context


class CustomListItemsProvider(AbstractChildrenProvider):
    def __init__(self, items_provider: TypeVizItemProviderCustomListItems, instr: CustomListItemsInstruction, size: Optional[int],
                 ctx_val: lldb.SBValue, context: EvaluationContext, wildcards: Sequence[str]):
        self._items_provider: TypeVizItemProviderCustomListItems = items_provider
        self._next_instruction: CustomListItemsInstruction = instr
        self._ctx_val: lldb.SBValue = ctx_val
        self._context: EvaluationContext = context
        self._wildcards: Sequence[str] = wildcards

        self.cached_items: List[lldb.SBValue] = list()
        self.size: int = 0
        self.name_to_item: dict[str, int] = dict()
        if size is not None:
            # Cache will be calculated lazily
            self.size = size
        else:
            self._calculate_cache(g_max_num_children)
            self.size = len(self.cached_items)

    def _calculate_cache(self, stop_at: int) -> None:
        first_index = len(self.cached_items)
        while self._next_instruction and len(self.cached_items) <= stop_at:
            self._next_instruction = self._next_instruction.execute(self._ctx_val, self._context, self.cached_items)
        for idx in range(first_index, len(self.cached_items)):
            self.name_to_item[self.cached_items[idx].GetName()] = idx

    def num_children(self) -> int:
        return self.size

    def get_child_index(self, name: str) -> int:
        try:
            return self.name_to_item[name]
        except KeyError:
            return INVALID_CHILD_INDEX

    def get_child_at_index(self, index: int) -> lldb.SBValue:
        if index >= len(self.cached_items):
            self._calculate_cache(index)

        return self.cached_items[index]

    def try_update_size(self, value_non_synth: lldb.SBValue) -> ChildrenProviderUpdateResult:
        new_provider = _create_custom_list_items_provider(self._items_provider, value_non_synth, self._wildcards)
        if new_provider is None:
            # That probably means that this provider is no longer valid, and we should rebuild all providers. But that should be rare case.
            return ChildrenProviderUpdateResult.NONE

        old_size = self.size
        self._next_instruction = new_provider._next_instruction
        self._ctx_val = new_provider._ctx_val
        self._context = new_provider._context
        self.cached_items = new_provider.cached_items
        self.size = new_provider.size
        self.name_to_item = new_provider.name_to_item
        return ChildrenProviderUpdateResult.SIZE_UPDATED if old_size != self.size else ChildrenProviderUpdateResult.NONE


g_node_to_evaluation_context_factory = {}


@optional_node_processor
def _create_custom_list_items_provider(items_provider: TypeVizItemProviderCustomListItems, ctx_val: lldb.SBValue,
                                       wildcards: Sequence[str]) -> Optional[CustomListItemsProvider]:
    if items_provider.condition:
        if not _process_node_condition(items_provider.condition, ctx_val, wildcards):
            return None

    root_instr = _process_code_block_nodes(items_provider.code_block_nodes, wildcards, None, [])
    size = _calculate_items_provider_size(items_provider.size_nodes, ctx_val, wildcards)
    instantiated_node = (items_provider, wildcards)
    if instantiated_node not in g_node_to_evaluation_context_factory:
        context_factory = _process_variables_nodes(items_provider.variables_nodes, wildcards)
        g_node_to_evaluation_context_factory[instantiated_node] = context_factory
        context = context_factory(ctx_val, True)
    else:
        context_factory = g_node_to_evaluation_context_factory[instantiated_node]
        context = context_factory(ctx_val, False)

    return CustomListItemsProvider(items_provider, root_instr, size, ctx_val, context, wildcards)


def _process_item_provider_custom_list_items(items_provider, val, wildcards):
    return _create_custom_list_items_provider(items_provider, val, wildcards)


def _build_child_providers(item_providers: list,
                           value_non_synth: lldb.SBValue,
                           wildcards: tuple,
                           hide_raw_view: bool) -> list[AbstractChildrenProvider]:
    provider_handlers = {
        TypeVizItemProviderTypeKind.Single: _process_item_provider_single,
        TypeVizItemProviderTypeKind.Expanded: _process_item_provider_expanded,
        TypeVizItemProviderTypeKind.ArrayItems: _process_item_provider_array_items,
        TypeVizItemProviderTypeKind.IndexListItems: _process_item_provider_index_list_items,
        TypeVizItemProviderTypeKind.LinkedListItems: _process_item_provider_linked_list_items,
        TypeVizItemProviderTypeKind.TreeItems: _process_item_provider_tree_items,
        TypeVizItemProviderTypeKind.CustomListItems: _process_item_provider_custom_list_items,
    }
    child_providers = []
    for item_provider in item_providers:
        handler = provider_handlers.get(item_provider.kind)
        if not handler:
            continue
        child_provider = handler(item_provider, value_non_synth, wildcards)
        if not child_provider:
            continue
        child_providers.append(child_provider)

    if not hide_raw_view and (value_non_synth.GetFormat() & eFormatNoRawView) == 0:
        child_providers.append(RawViewItemProvider(value_non_synth))

    return child_providers
