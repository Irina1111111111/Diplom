from __future__ import annotations

from typing import Sequence

import lldb
from jb_declarative_formatters.type_viz_intrinsic import TypeVizIntrinsic, IntrinsicsScope
from renderers.jb_lldb_cache import LLDBCache
from renderers.jb_lldb_logging import log
from renderers.jb_lldb_evaluation_utils import EvaluateError, prepare_default_lldb_expression_options, resolve_type_wildcards


class IntrinsicsPrologCache:
    """
    IntrinsicsPrologCache may calculate the intrinsics prolog for
    a specific type in the current module before the expression is evaluated.
    It caches the calculated prologs and clears them when
    modules are loaded or unloaded, and when new symbols are loaded.
    """
    _lldb_cache = LLDBCache("lldb.IntrinsicsPrologCache",
                            lldb.SBTarget.eBroadcastBitModulesLoaded |
                            lldb.SBTarget.eBroadcastBitModulesUnloaded |
                            lldb.SBTarget.eBroadcastBitSymbolsLoaded)

    _global_intrinsic_scope: IntrinsicsScope | None = None
    _type_intrinsic_scope: IntrinsicsScope | None = None
    _type_wildcards: Sequence[str] = []

    _previous_global_intrinsic_scope: IntrinsicsScope | None = None
    _previous_type_intrinsic_scope: IntrinsicsScope | None = None
    _previous_type_wildcards: Sequence[str] = []

    @staticmethod
    def _build_prolog_from_intrinsic_list(intrinsic_list: list[TypeVizIntrinsic]) -> str:
        result = '\n'.join(intrinsic.get_definition_code() for intrinsic in intrinsic_list)
        return result

    @staticmethod
    def _validate_error(result: lldb.SBValue) -> tuple[bool, lldb.SBError | None]:
        if result is None:
            err = lldb.SBError()
            err.SetErrorString("Evaluation setup failed")
            return False, err
        error = result.GetError()
        if error.Fail():
            return False, error

        return True, None

    @classmethod
    def _fill_intrinsic_list_from_scope(cls, lldb_val: lldb.SBValue, scope: IntrinsicsScope | None,
                                        skip_unused: bool, result_intrinsics: list[TypeVizIntrinsic]) -> None:
        if not scope:
            return

        for intrinsic in scope.sorted_list:
            if skip_unused and not intrinsic.is_used:
                continue  # like VS, we can skip the global intrinsic

            dependencies_init_code = cls._build_prolog_from_intrinsic_list(result_intrinsics)
            intrinsic_check_code = intrinsic.get_code_for_validate(dependencies_init_code)
            if not intrinsic_check_code:
                continue

            code = resolve_type_wildcards(f"{intrinsic_check_code}; 1", cls._type_wildcards)
            result: lldb.SBValue = lldb_val.EvaluateExpression(code, prepare_default_lldb_expression_options())

            success, error = cls._validate_error(result)
            if not success:
                type_name = lldb_val.GetTypeName()
                if intrinsic.optional:
                    log("Ignoring error on evaluating optional the intrinsic '{}' with expression '{}' on object '{}'."
                        " Error: {}",
                        intrinsic.name, intrinsic.expression, type_name, str(error))
                    continue
                log("Error on evaluating the intrinsic '{}' with expression '{}' on object '{}'. Error: {}",
                    intrinsic.name, intrinsic.expression, type_name, str(error))
                raise EvaluateError(error)

            replaced = False
            for idx, item in enumerate(result_intrinsics):
                if intrinsic.name == item.name:
                    result_intrinsics[idx] = intrinsic
                    replaced = True
            if not replaced:
                result_intrinsics.append(intrinsic)

    @classmethod
    def _prepare_intrinsics_prolog(cls, val: lldb.SBValue) -> str:
        type_intrinsics: list[TypeVizIntrinsic] = []

        cls._fill_intrinsic_list_from_scope(val, cls._global_intrinsic_scope,
                                            skip_unused=True, result_intrinsics=type_intrinsics)
        cls._fill_intrinsic_list_from_scope(val, cls._type_intrinsic_scope,
                                            skip_unused=False, result_intrinsics=type_intrinsics)

        return cls._build_prolog_from_intrinsic_list(type_intrinsics)

    @classmethod
    def update_current_intrinsics_scope(cls, global_intrinsic_scope: IntrinsicsScope | None,
                                        type_intrinsic_scope: IntrinsicsScope | None,
                                        type_wildcards: Sequence[str]) -> None:
        # TODO: Instead of storing intrinsics scope in a global variable,
        #  we could pass the scope to the evaluation, but it requires some extra refactoring.
        cls._previous_global_intrinsic_scope = cls._global_intrinsic_scope
        cls._previous_type_intrinsic_scope = cls._type_intrinsic_scope
        cls._previous_type_wildcards = cls._type_wildcards

        cls._global_intrinsic_scope = global_intrinsic_scope
        cls._type_intrinsic_scope = type_intrinsic_scope
        cls._type_wildcards = type_wildcards

    @classmethod
    def rollback_current_intrinsics_scope(cls):
        cls._global_intrinsic_scope = cls._previous_global_intrinsic_scope
        cls._type_intrinsic_scope = cls._previous_type_intrinsic_scope
        cls._type_wildcards = cls._previous_type_wildcards

    @classmethod
    def add_intrinsics_prolog(cls, val: lldb.SBValue, expression: str) -> str:
        has_global_intrinsics = bool(cls._global_intrinsic_scope and cls._global_intrinsic_scope.sorted_list)
        has_type_intrinsics = bool(cls._type_intrinsic_scope and cls._type_intrinsic_scope.sorted_list)
        if not has_global_intrinsics and not has_type_intrinsics:
            return expression

        current_module_path = val.GetFrame().GetModule().GetPlatformFileSpec().fullpath or ''
        current_process = val.GetProcess()
        cache_key = (current_module_path, val.GetTypeName())
        intrinsic_prolog = cls._lldb_cache.get_for_process(current_process, cache_key)

        if intrinsic_prolog is None:
            intrinsic_prolog_raw = cls._prepare_intrinsics_prolog(val)
            intrinsic_prolog = resolve_type_wildcards(intrinsic_prolog_raw, cls._type_wildcards)
            cls._lldb_cache.set_for_process(current_process, cache_key, intrinsic_prolog)

        if intrinsic_prolog:
            expression = f"{intrinsic_prolog}\n\n{expression}"

        return expression
