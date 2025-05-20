import re
from typing import List, Tuple


class TypeVizTypeTraits:
    class StringTraits:
        def __init__(self, type_prefixes: List[str], char_type: str, strncmp: str, strlen: str):
            self.type_prefixes = type_prefixes
            self.char_type = char_type
            self.strncmp = strncmp
            self.strlen = strlen

    _SUPPORTED_STRING_TYPES = [
        StringTraits(
            ["std::basic_string<char,", "std::basic_string_view<(char),"],
            "char", "::__builtin_strncmp", "::__builtin_strlen"
        ),
        StringTraits(
            ["std::basic_string<wchar_t,", "std::basic_string_view<(wchar_t),"],
            "wchar_t", "::__builtin_wcsncmp", "::__builtin_wcslen"
        ),
        StringTraits(
            ["TStringView<ANSICHAR>", "TStringView<(char)>"],
            "char", "::_strnicmp", "::__builtin_strlen"
        ),
        StringTraits(
            ["TStringView<WIDECHAR>", "TStringView<(wchar_t)>", "FString"],
            "wchar_t", "::_wcsnicmp", "::__builtin_wcslen"
        )
    ]

    _STRING_TYPES_SPECIALIZATIONS = [
        ("std::basic_string_view<(.*),", ["std::basic_string_view<(char),", "std::basic_string_view<(wchar_t),"]),
        ("TStringView<(.*)>", ["TStringView<(char)>", "TStringView<(wchar_t)>"])
    ]

    @classmethod
    def get_string_type_traits(cls, type_name: str) -> List[Tuple[str, StringTraits]]:
        for (type_prefix, specializations) in cls._STRING_TYPES_SPECIALIZATIONS:
            if type_name.startswith(type_prefix):
                spec_types = []
                for spec in specializations:
                    spec_types += cls.get_string_type_traits(type_name.replace(type_prefix, spec, 1))
                return spec_types

        matched_traits = []
        for type_trait in cls._SUPPORTED_STRING_TYPES:
            for type_prefix in type_trait.type_prefixes:
                if type_name.startswith(type_prefix):
                    matched_traits.append((type_name, type_trait))
        return matched_traits

    _REQUIRED_SUBSCRIPT_OPERATOR_TYPES = ["std::basic_string", "TArray", "TBitArray", "TMulticastDelegate"]
    _REQUIRED_SUBSCRIPT_OPERATOR_TYPES_PATTERN = re.compile(f"^(:?{'|'.join(_REQUIRED_SUBSCRIPT_OPERATOR_TYPES)})")

    @classmethod
    def is_subscript_operator_required(cls, type_name: str) -> bool:
        return cls._REQUIRED_SUBSCRIPT_OPERATOR_TYPES_PATTERN.match(type_name) is not None
