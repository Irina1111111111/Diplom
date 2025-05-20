from __future__ import annotations

from typing import Optional

import lldb
from jb_declarative_formatters.parsers.cpp_parser import CppParser
from jb_declarative_formatters.type_viz_synthetic_method import SyntheticMethod


class ItemExpression:
    _EXPRESSION_KEY = "com.jetbrains.item.expression"
    _MAX_EXPRESSION_LENGTH = 1024

    INVALID_EXPRESSION = "/* Cannot make a path to the item. */"

    @classmethod
    def _get_type_expression(cls, class_type: lldb.SBType, original_type_name: str | None = None) -> str:
        type_name = original_type_name or class_type.GetName()
        match class_type.GetTypeClass():
            case lldb.eTypeClassClass:
                return CppParser.insert_type_class_specifier(type_name, "class")
            case lldb.eTypeClassEnumeration:
                return CppParser.insert_type_class_specifier(type_name, "enum")
            case lldb.eTypeClassStruct:
                return CppParser.insert_type_class_specifier(type_name, "struct")
            case lldb.eTypeClassUnion:
                return CppParser.insert_type_class_specifier(type_name, "union")
            case lldb.eTypeClassReference:
                return cls._get_type_expression(class_type.GetDereferencedType(), type_name)
            case lldb.eTypeClassPointer:
                return cls._get_type_expression(class_type.GetPointeeType(), type_name)
            case lldb.eTypeClassArray:
                return cls._get_type_expression(class_type.GetArrayElementType(), type_name)
            case lldb.eTypeClassVector:
                return cls._get_type_expression(class_type.GetVectorElementType(), type_name)
            case _:
                return type_name

    @classmethod
    def _save_item_expression(cls, non_synthetic_value: lldb.SBValue, expression: str) -> str:
        if len(expression) >= cls._MAX_EXPRESSION_LENGTH:
            expression = cls._as_raw_reference(non_synthetic_value)

        if non_synthetic_value.IsDynamic():
            non_synthetic_static_value = non_synthetic_value.GetStaticValue().GetNonSyntheticValue()
            if non_synthetic_static_value.IsValid():
                non_synthetic_static_value.SetMetadata(cls._EXPRESSION_KEY, expression)
                dynamic_type: lldb.SBType = non_synthetic_value.GetType()
                static_type_name = non_synthetic_static_value.GetTypeName()
                if expression != cls.INVALID_EXPRESSION and static_type_name != dynamic_type.GetName():
                    is_ptr = dynamic_type.IsPointerType()
                    is_ref = dynamic_type.IsReferenceType()
                    original_dynamic_type = dynamic_type
                    if is_ptr:
                        original_dynamic_type = dynamic_type.GetPointeeType()
                    elif is_ref:
                        original_dynamic_type = dynamic_type.GetDereferencedType()
                    if cls._is_cast_allowed(original_dynamic_type):
                        ref_char = "" if is_ptr or is_ref else " &"
                        type_expr = cls._get_type_expression(dynamic_type)
                        expression = f"(({type_expr}{ref_char}) {expression})"
                    else:
                        expression = cls.INVALID_EXPRESSION

        non_synthetic_value.SetMetadata(cls._EXPRESSION_KEY, expression)
        return expression

    @classmethod
    def _get_or_create_expression(cls, non_synthetic_value: lldb.SBValue) -> str:
        expression = non_synthetic_value.GetMetadata(cls._EXPRESSION_KEY)
        if expression:
            return expression
        if non_synthetic_value.IsDynamic():
            non_synthetic_static_value: lldb.SBValue = non_synthetic_value.GetStaticValue().GetNonSyntheticValue()
            static_value_expression = non_synthetic_static_value.GetMetadata(cls._EXPRESSION_KEY)
            if static_value_expression:
                return cls._save_item_expression(non_synthetic_value, static_value_expression)
        path_expression = non_synthetic_value.path
        if path_expression:
            return cls._save_item_expression(non_synthetic_value, path_expression)
        return cls._save_item_expression(non_synthetic_value, cls._as_raw_reference(non_synthetic_value))

    @classmethod
    def _is_cast_allowed(cls, value_type: lldb.SBType) -> bool:
        if value_type.IsAnonymousType():
            return False
        return not CppParser.has_lambda_in_type_expr(value_type.GetName())

    @classmethod
    def _update_dereference_metadata(cls, value_deref: lldb.SBValue, value_ptr: lldb.SBValue, allow_deref_star: bool):
        non_synthetic_value_deref = value_deref.GetNonSyntheticValue()
        non_synthetic_value_ptr: lldb.SBValue = value_ptr.GetNonSyntheticValue()
        if not non_synthetic_value_deref.IsValid() or not non_synthetic_value_ptr.IsValid():
            return
        expression = cls._get_or_create_expression(non_synthetic_value_ptr)
        value_ptr_is_pointer = non_synthetic_value_ptr.TypeIsPointerType()
        if expression != cls.INVALID_EXPRESSION:
            deref_star = "*" if allow_deref_star and value_ptr_is_pointer else ""
            if deref_star:
                deref_expr = CppParser.try_merge_deref_and_address_of(f"({deref_star}{expression})")
                cls._save_item_expression(non_synthetic_value_deref, deref_expr)
            else:
                cls._save_item_expression(non_synthetic_value_deref, expression)
            return

        if value_ptr_is_pointer and not cls._is_cast_allowed(non_synthetic_value_deref.GetType()):
            type_expr = cls._get_type_expression(non_synthetic_value_ptr.GetType())
            raw_ptr_deref = f"(*({type_expr}){non_synthetic_value_ptr.value})"
            cls._save_item_expression(non_synthetic_value_deref, raw_ptr_deref)
            return

        cls._save_item_expression(non_synthetic_value_deref, cls.INVALID_EXPRESSION)

    @classmethod
    def _as_raw_reference(cls, non_synthetic_value: lldb.SBValue):
        value_type: lldb.SBType = non_synthetic_value.GetType()
        if cls._is_cast_allowed(value_type):
            address: lldb.SBAddress = non_synthetic_value.GetAddress()
            if address.IsValid():
                type_expr = cls._get_type_expression(value_type.GetPointerType())
                return f"(*({type_expr})({address.__hex__()}))"
            if value_type.IsPointerType():
                type_expr = cls._get_type_expression(value_type)
                return f"(({type_expr})({non_synthetic_value.value}))"

        return cls.INVALID_EXPRESSION

    @classmethod
    def _get_this_reference(cls, non_synthetic_value: lldb.SBValue) -> str:
        this_ref = cls._get_or_create_expression(non_synthetic_value)
        if this_ref != cls.INVALID_EXPRESSION:
            return this_ref

        return cls._as_raw_reference(non_synthetic_value)

    @classmethod
    def _explicit_up_cast_for_base_class(cls, inheritor_class_value: lldb.SBValue, base_class_value: lldb.SBValue):
        non_synthetic_inheritor_class_value = inheritor_class_value.GetNonSyntheticValue()
        non_synthetic_base_class_value = base_class_value.GetNonSyntheticValue()
        if not non_synthetic_inheritor_class_value.IsValid() or not non_synthetic_base_class_value.IsValid():
            return

        base_class_type: lldb.SBType = base_class_value.GetType()
        if base_class_type.IsAnonymousType():
            cls.copy_item_expression(non_synthetic_inheritor_class_value, non_synthetic_base_class_value)
            return

        this_ref = cls._get_this_reference(non_synthetic_inheritor_class_value)
        if this_ref == cls.INVALID_EXPRESSION:
            cls._save_item_expression(non_synthetic_base_class_value, cls.INVALID_EXPRESSION)
            return

        type_expr = cls._get_type_expression(base_class_type)
        cls._save_item_expression(non_synthetic_base_class_value, f"(({type_expr} &) {this_ref})")

    @classmethod
    def dereference(cls, value_ptr: lldb.SBValue) -> lldb.SBValue:
        value_deref = value_ptr.Dereference()
        cls._update_dereference_metadata(value_deref, value_ptr, True)
        return value_deref

    @classmethod
    def cast_value_to_array(cls, non_synthetic_value: lldb.SBValue, is_array: bool, array_size: int) -> lldb.SBValue:
        val_type = non_synthetic_value.GetType()
        if is_array:
            elem_type = val_type.GetArrayElementType()
            non_synthetic_value = non_synthetic_value.AddressOf()
        else:
            elem_type = val_type.GetPointeeType()

        upd_val_type = elem_type.GetArrayType(array_size).GetPointerType()
        casted_ptr: lldb.SBValue = non_synthetic_value.Cast(upd_val_type)
        cls.copy_item_expression(non_synthetic_value, casted_ptr)

        value_deref: lldb.SBValue = casted_ptr.Dereference()
        cls._update_dereference_metadata(value_deref, casted_ptr, is_array)

        return value_deref

    @classmethod
    def array_address_of(cls, value_array: lldb.SBValue) -> lldb.SBValue:
        non_synthetic_value_array = value_array.GetNonSyntheticValue()
        value_ptr = non_synthetic_value_array.AddressOf()
        non_synthetic_value_ptr = value_ptr.GetNonSyntheticValue()
        if non_synthetic_value_ptr.IsValid() and non_synthetic_value_array.IsValid():
            cls._save_item_expression(non_synthetic_value_ptr, cls._get_or_create_expression(non_synthetic_value_array))

        return value_ptr

    @classmethod
    def cast_value_to_basic_type_pointer(cls, non_synthetic_value: lldb.SBValue, basic_type: int) -> lldb.SBValue:
        original_type: lldb.SBType = non_synthetic_value.GetType()
        new_pointer_type: lldb.SBValue = original_type.GetBasicType(basic_type).GetPointerType()
        casted_pointer: lldb.SBValue = non_synthetic_value.Cast(new_pointer_type)
        non_synthetic_casted_pointer: lldb.SBValue = casted_pointer.GetNonSyntheticValue()
        original_expression = cls._get_or_create_expression(non_synthetic_value)
        pointer_type_name = non_synthetic_casted_pointer.GetTypeName()
        if original_expression == cls.INVALID_EXPRESSION:
            if non_synthetic_casted_pointer.IsValid():
                cls._save_item_expression(non_synthetic_casted_pointer, f"(({pointer_type_name}) {non_synthetic_casted_pointer.value})")
            else:
                cls._save_item_expression(non_synthetic_casted_pointer, cls.INVALID_EXPRESSION)
        else:
            cls._save_item_expression(non_synthetic_casted_pointer, f"(({pointer_type_name}) {original_expression})")
        return non_synthetic_casted_pointer

    @classmethod
    def update_struct_child_item_expression(cls, child_value: lldb.SBValue, struct_value: lldb.SBValue):
        if struct_value.TypeIsPointerType():
            cls._update_dereference_metadata(child_value, struct_value, True)
            return
        if child_value.GetType().IsAnonymousType():
            cls.copy_item_expression(struct_value, child_value)
            return
        if struct_value.path == child_value.path:
            cls._explicit_up_cast_for_base_class(struct_value, child_value)
            return

        child_name = child_value.GetName()
        if child_name is None:
            cls.invalidate_item_expression(child_value)
        else:
            cls.update_item_expression(child_value, struct_value, child_name)

    @classmethod
    def copy_item_expression(cls, from_value: lldb.SBValue, to_value: lldb.SBValue):
        non_synthetic_from_value = from_value.GetNonSyntheticValue()
        non_synthetic_to_value = to_value.GetNonSyntheticValue()
        if non_synthetic_to_value.IsValid() and non_synthetic_from_value.IsValid():
            cls._save_item_expression(non_synthetic_to_value, cls._get_or_create_expression(non_synthetic_from_value))

    @classmethod
    def invalidate_item_expression(cls, value: lldb.SBValue):
        non_synthetic: lldb.SBValue = value.GetNonSyntheticValue()
        if non_synthetic.IsValid():
            cls._save_item_expression(non_synthetic, cls.INVALID_EXPRESSION)

    @classmethod
    def update_item_expression(cls, item_value: lldb.SBValue, context_value: lldb.SBValue, expression: str,
                               getter_call: Optional[SyntheticMethod.Call] = None):
        non_synthetic_item_value: lldb.SBValue = item_value.GetNonSyntheticValue()
        non_synthetic_context_value: lldb.SBValue = context_value.GetNonSyntheticValue()
        if not non_synthetic_item_value.IsValid() or not non_synthetic_context_value.IsValid():
            return

        simplified_expression = CppParser.simplify_cpp_expression(expression)

        this_ref = cls._get_this_reference(non_synthetic_context_value)
        if this_ref == cls.INVALID_EXPRESSION:
            cls._save_item_expression(non_synthetic_item_value, cls._as_raw_reference(non_synthetic_item_value))
            return
        if simplified_expression == "this":
            cls._save_item_expression(non_synthetic_item_value, f"(&{this_ref})")
            return
        if getter_call is not None:
            cls._save_item_expression(non_synthetic_item_value, getter_call.make_call_expr(this_ref))
            return
        if CppParser.is_array_access_expr(simplified_expression):
            cls._save_item_expression(non_synthetic_item_value, f"{this_ref}{simplified_expression}")
            return
        if CppParser.is_trivial_expression(simplified_expression):
            cls._save_item_expression(non_synthetic_item_value, f"{this_ref}.{simplified_expression}")
            return
        specifier, sub_expression = CppParser.cut_deref_or_address_of_from_trivial_expression(simplified_expression)
        if specifier and simplified_expression:
            cls._save_item_expression(non_synthetic_item_value, f"({specifier}({this_ref}.{sub_expression}))")
            return
        cls._save_item_expression(non_synthetic_item_value, cls._as_raw_reference(non_synthetic_item_value))
