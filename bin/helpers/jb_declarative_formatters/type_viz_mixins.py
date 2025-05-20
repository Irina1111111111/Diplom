from typing import Optional

from .type_viz_expression import TypeVizExpression, TypeVizCondition
from .type_viz_synthetic_method import SyntheticMethod


class TypeVizItemConditionalNodeMixin(object):
    def __init__(self, condition: TypeVizCondition, *args, **kwargs):
        super(TypeVizItemConditionalNodeMixin, self).__init__(*args, **kwargs)
        self.condition: TypeVizCondition = condition


class TypeVizItemOptionalNodeMixin(object):
    def __init__(self, optional, *args, **kwargs):
        super(TypeVizItemOptionalNodeMixin, self).__init__(*args, **kwargs)
        self.optional = optional


class TypeVizItemNamedNodeMixin(object):
    def __init__(self, name, *args, **kwargs):
        super(TypeVizItemNamedNodeMixin, self).__init__(*args, **kwargs)
        self.name = name


class TypeVizItemFormattedExpressionNodeMixin(object):
    def __init__(self, expr, *args, **kwargs):
        super(TypeVizItemFormattedExpressionNodeMixin, self).__init__(*args, **kwargs)
        assert (isinstance(expr, TypeVizExpression))
        self.expr = expr


class TypeVizItemValueNodeMixin(object):
    def __init__(self, text, *args, **kwargs):
        super(TypeVizItemValueNodeMixin, self).__init__(*args, **kwargs)
        self.text = text


class TypeVizItemSyntheticGetterNodeMixin(object):
    def __init__(self, *args, **kwargs):
        super(TypeVizItemSyntheticGetterNodeMixin, self).__init__(*args, **kwargs)
        self.synthetic_getter: Optional[SyntheticMethod] = None
