from __future__ import annotations

from enum import Enum, auto
from typing import Optional, List

from jb_declarative_formatters.type_name_template import TypeNameTemplate
from jb_declarative_formatters.type_viz_expression import get_custom_view_spec_id_by_name, TypeVizExpression, \
    TypeVizCondition
from jb_declarative_formatters.type_viz_intrinsic import IntrinsicsScope
from lldb.formatters.Logger import Logger


class TypeVizName(object):
    def __init__(self, type_name: str, type_name_template: TypeNameTemplate):
        self.type_name: str = type_name
        self.type_name_template: TypeNameTemplate = type_name_template

    @property
    def has_wildcard(self) -> bool:
        return self.type_name_template.has_wildcard

    def __str__(self) -> str:
        return self.type_name


class TypeVizSmartPointer(object):
    class Usage(Enum):
        Minimal = auto()
        Indexable = auto()
        # There is one more usage: 'Full'.
        # The 'Full' usage means that the smart pointer contains a conversion operator to the underlying pointer.
        # LLDB is unable to declare the conversion operator properly. TODO: It can be fixed in LLDB.
        # Neither 'stl.natvis' nor 'Unreal.natvis' contains the 'Full' usage; therefore, consider 'Full' as 'Indexed'.

    def __init__(self, expression: TypeVizExpression, usage: Usage):
        self.expression = expression
        self.usage = usage


class TypeVizStringView(object):
    def __init__(self, expression: TypeVizExpression, condition: TypeVizCondition):
        self.expression = expression
        self.condition = condition


class TypeViz(object):
    def __init__(self,
                 type_viz_names: list[TypeVizName],
                 is_inheritable: bool,
                 include_view: str,
                 exclude_view: str,
                 priority: int,
                 global_intrinsics: IntrinsicsScope,
                 type_intrinsics: IntrinsicsScope,
                 logger: Logger = None):
        self.logger = logger  # TODO: or stub

        self.type_viz_names = type_viz_names
        self.is_inheritable = is_inheritable
        self.include_view = include_view
        self.include_view_id = get_custom_view_spec_id_by_name(include_view)
        self.exclude_view = exclude_view
        self.exclude_view_id = get_custom_view_spec_id_by_name(exclude_view)
        self.priority = priority
        self.summaries = []
        self.item_providers = None
        self.global_intrinsics = global_intrinsics
        self.type_intrinsics = type_intrinsics
        self.hide_raw_view: bool = False
        self.smart_pointer: Optional[TypeVizSmartPointer] = None
        self.string_views: List[TypeVizStringView] = []
