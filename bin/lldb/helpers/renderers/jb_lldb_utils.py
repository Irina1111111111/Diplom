from __future__ import annotations

import traceback
from enum import Flag, auto
from typing import Optional

import lldb
from renderers.jb_lldb_declarative_formatters_options import set_recursion_level
from renderers.jb_lldb_evaluation_utils import EvalSettings, EvaluateError, EvaluationContext
from renderers.jb_lldb_format_specs import eFormatRawView
from renderers.jb_lldb_intrinsics_prolog_cache import IntrinsicsPrologCache
from renderers.jb_lldb_logging import log
from renderers.jb_lldb_item_expression import ItemExpression
from six import StringIO


class IgnoreSynthProvider(Exception):
    def __init__(self, msg=None):
        super(Exception, self).__init__(str(msg) if msg else None)


class Stream(object):
    def __init__(self, is64bit: bool, initial_level: int):
        self.stream = StringIO()
        self.pointer_format = "0x{:016x}" if is64bit else "0x{:08x}"
        self.length = 0
        self.level = initial_level

    def create_nested(self):
        val = self.__class__(False, self.level)
        val.pointer_format = self.pointer_format
        val.length = self.length
        return val

    def output(self, text):
        self.length += len(text)
        self.stream.write(text)

    def output_object(self, val_non_synth: lldb.SBValue):
        log("Retrieving summary of value named '{}'...", val_non_synth.GetName())

        val_type = val_non_synth.GetType()
        format_spec = val_non_synth.GetFormat()
        provider = get_viz_descriptor_provider()
        vis_descriptor = provider.get_matched_visualizers(val_type, format_spec)

        self.level += 1
        prev_level = set_recursion_level(self.level)
        try:
            if vis_descriptor is not None:
                try:
                    vis_descriptor.output_summary(val_non_synth, self)
                except Exception as e:
                    log('Internal error: {}, traceback: {}', str(e), traceback.format_exc())

            else:
                self._output_object_fallback(provider, val_non_synth, val_type)
        finally:
            set_recursion_level(prev_level)
            self.level -= 1

    def _output_object_fallback(self, provider: AbstractVizDescriptorProvider, val_non_synth: lldb.SBValue, val_type: lldb.SBType):
        # force use raw vis descriptor
        vis_descriptor = provider.get_matched_visualizers(val_type, eFormatRawView)
        if vis_descriptor is not None:
            try:
                vis_descriptor.output_summary(val_non_synth, self)
            except Exception as e:
                log('Internal error: {}', str(e))
        else:
            summary_value = val_non_synth.GetValue() or ''
            self.output(summary_value)

    def output_string(self, text: str):
        self.output(text)

    def output_keyword(self, text: str):
        self.output(text)

    def output_number(self, text: str):
        self.output(text)

    def output_comment(self, text: str):
        self.output(text)

    def output_value(self, text: str):
        self.output(text)

    def output_address(self, address: int):
        self.output_comment(self.pointer_format.format(address))

    def __str__(self):
        return self.stream.getvalue()


INVALID_CHILD_INDEX = 2 ** 32 - 1


class ChildrenProviderUpdateResult(Flag):
    NONE = 0
    SIZE_UPDATED = auto()


class AbstractChildrenProvider(object):
    def num_children(self) -> int:
        return 0

    def get_child_index(self, name: str) -> int:
        return INVALID_CHILD_INDEX

    def get_child_at_index(self, index: int) -> lldb.SBValue:
        raise NotImplementedError

    def try_update_size(self, value_non_synth: lldb.SBValue) -> ChildrenProviderUpdateResult:
        return ChildrenProviderUpdateResult.NONE


g_empty_children_provider = AbstractChildrenProvider()


class AbstractVisDescriptor(object):
    def output_summary(self, value_non_synth: lldb.SBValue, stream: Stream):
        pass

    def prepare_children(self, value_non_synth: lldb.SBValue) -> AbstractChildrenProvider:
        return g_empty_children_provider


class AbstractVizDescriptorProvider(object):
    def get_matched_visualizers(self, value_type: lldb.SBType, format_spec: int) -> AbstractVisDescriptor:
        pass


g_viz_descriptor_provider: AbstractVizDescriptorProvider


def get_viz_descriptor_provider() -> AbstractVizDescriptorProvider:
    return g_viz_descriptor_provider


def set_viz_descriptor_provider(provider: AbstractVizDescriptorProvider):
    global g_viz_descriptor_provider
    g_viz_descriptor_provider = provider


class FormattedStream(Stream):
    def output_string(self, text):
        self.stream.write("\xfeS")
        self.output(text)
        self.stream.write("\xfeE")

    def output_keyword(self, text):
        self.stream.write("\xfeK")
        self.output(text)
        self.stream.write("\xfeE")

    def output_number(self, text):
        self.stream.write("\xfeN")
        self.output(text)
        self.stream.write("\xfeE")

    def output_comment(self, text):
        self.stream.write("\xfeC")
        self.output(text)
        self.stream.write("\xfeE")

    def output_value(self, text):
        self.stream.write("\xfeV")
        self.output(text)
        self.stream.write("\xfeE")


def make_absolute_name(root, name):
    return '.'.join([root, name])


def register_lldb_commands(debugger, cmd_map):
    for func, cmd in cmd_map.items():
        debugger.HandleCommand('command script add -f {func} {cmd}'.format(func=func, cmd=cmd))


def _execute_lldb_eval(val: lldb.SBValue, code: str, user_eval_settings: Optional[EvalSettings]) -> lldb.SBValue:
    eval_settings = user_eval_settings or EvalSettings()
    result = val.EvaluateExpression(code, eval_settings.options, eval_settings.name)
    if result is None:
        err = lldb.SBError()
        err.SetErrorString("evaluation setup failed")
        log("Evaluate failed: {}", str(err))
        raise EvaluateError(err)
    if eval_settings.save_expression_in_metadata:
        ItemExpression.update_item_expression(result, val, code, eval_settings.getter_call)
    elif eval_settings.name is not None:
        ItemExpression.invalidate_item_expression(result)
    return result


def eval_expression(val: lldb.SBValue, expr: str, settings: Optional[EvalSettings] = None,
                    context: Optional[EvaluationContext] = None) -> lldb.SBValue:
    log("Evaluate '{}' in context of '{}' of type '{}'", expr, val.GetName(), val.GetTypeName())

    expression_with_context = context.add_context(expr) if context else expr
    expression_with_intrinsics = IntrinsicsPrologCache.add_intrinsics_prolog(val, expression_with_context)
    eval_result = _execute_lldb_eval(val, expression_with_intrinsics, settings)

    result_non_synth = eval_result.GetNonSyntheticValue()
    err: lldb.SBError = result_non_synth.GetError()
    if err.Fail():
        err_type = err.GetType()
        err_code = err.GetError()
        if err_type == lldb.eErrorTypeExpression and err_code == lldb.eExpressionParseError:
            log("Evaluate failed (can't parse expression): {}", str(err))
            raise EvaluateError(err)

        # error is runtime error which is handled later
        log("Returning value with error: {}", str(err))
        return eval_result

    log("Evaluate succeed: result type - {}", str(result_non_synth.GetTypeName()))
    return eval_result


def get_root_value(val: lldb.SBValue) -> lldb.SBValue:
    val_non_synth: lldb.SBValue = val.GetNonSyntheticValue()
    val_non_synth.SetPreferDynamicValue(lldb.eNoDynamicValues)
    return val_non_synth


def get_value_format(val: lldb.SBValue) -> int:
    return get_root_value(val).GetFormat()


def set_value_format(val: lldb.SBValue, fmt: int):
    # noinspection PyArgumentList
    get_root_value(val).SetFormat(fmt)
