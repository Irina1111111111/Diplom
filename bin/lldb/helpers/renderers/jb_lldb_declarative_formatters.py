import distutils.util
import importlib
import inspect
import shlex
from typing import Iterable

from jb_declarative_formatters.parsers.cpp_parser import CppParser
from jb_declarative_formatters.parsers.type_name_parser import parse_type_name_template
from jb_declarative_formatters.type_viz_synthetic_method import SyntheticMethodDefinition
from jb_lldb_polyfills import \
    LLDBRemoveAllTopLevelLazyDeclarations, \
    LLDBAddTopLevelLazyDeclarationByRegex, \
    LLDBAddTopLevelLazyDeclaration
from renderers.jb_lldb_builtin_formatters import *
from renderers.jb_lldb_declarative_formatters_loaders import *
from renderers.jb_lldb_declarative_formatters_manager import *
from renderers.jb_lldb_format import update_value_dynamic_state
from renderers.jb_lldb_logging import get_suppress_errors
from renderers.jb_lldb_natvis_formatters import NatVisDescriptor

lldb_formatters_manager: FormattersManager


def __lldb_init_module(debugger: lldb.SBDebugger, internal_dict):
    log('JetBrains declarative formatters LLDB module registered into {}', str(debugger))

    commands_list = {
        make_absolute_name(__name__, '_cmd_loaders_add'): 'jb_renderers_loaders_add',
        make_absolute_name(__name__, '_cmd_loaders_remove'): 'jb_renderers_loaders_remove',
        make_absolute_name(__name__, '_cmd_loaders_list'): 'jb_renderers_loaders_list',

        make_absolute_name(__name__, '_cmd_load'): 'jb_renderers_load',
        make_absolute_name(__name__, '_cmd_remove'): 'jb_renderers_remove',
        make_absolute_name(__name__, '_cmd_reload'): 'jb_renderers_reload',

        make_absolute_name(__name__, '_cmd_reload_all'): 'jb_renderers_reload_all',
        make_absolute_name(__name__, '_cmd_remove_all'): 'jb_renderers_remove_all',
        make_absolute_name(__name__, '_cmd_list_all'):   'jb_renderers_list_all',

        make_absolute_name(__name__, '_cmd_override_charset'): 'jb_renderers_override_charset',
        make_absolute_name(__name__, '_cmd_set_markup'): 'jb_renderers_set_markup',
        make_absolute_name(__name__, '_cmd_set_global_hex'): 'jb_renderers_set_global_hex',
    }
    register_lldb_commands(debugger, commands_list)

    summary_func_name = f'{__name__}.declarative_summary'
    synth_class_name = f'{__name__}.DeclarativeSynthProvider'
    debugger.HandleCommand(f'type summary add -v -x ".*" -F {summary_func_name} -e --category jb_formatters')
    debugger.HandleCommand(f'type synthetic add -x ".*" -l {synth_class_name} --category jb_formatters')

    global lldb_formatters_manager
    lldb_formatters_manager = FormattersManager(summary_func_name, synth_class_name)

    viz_provider = VizDescriptorProvider()
    set_viz_descriptor_provider(viz_provider)


def _cmd_loaders_add(debugger, command, exe_ctx, result, internal_dict):
    # raise NotImplementedError("jb_renderers_loaders_add is not implemented yet")
    help_message = 'Usage: jb_renderers_loaders_add <loader_tag> <module> <funcname>'
    cmd = shlex.split(command)
    if len(cmd) < 1:
        result.SetError('Loader tag expected.\n{}'.format(help_message))
        return
    tag = cmd[0]
    cmd = cmd[1:]
    if len(cmd) < 1:
        result.SetError('Python module expected.\n{}'.format(help_message))
        return
    module = cmd[0]

    try:
        mod = importlib.import_module(module)
    except Exception as e:
        result.SetError(str(e))
        return

    cmd = cmd[1:]
    if len(cmd) < 1:
        result.SetError('Function name expected.\n{}'.format(help_message))
        return
    func_name = cmd[0]

    funcs = inspect.getmembers(mod, lambda m: inspect.isfunction(m) and m.__name__ == func_name)
    if funcs is None or len(funcs) == 0:
        result.SetError('Can\'t find loader function {} in module {}'.format(func_name, mod))
        return

    if len(funcs) != 1:
        result.SetError('Loader function {} in module {} is ambiguous'.format(func_name, mod))
        return

    _, func = funcs[0]
    type_viz_loader_add(tag, func)


def _cmd_loaders_remove(debugger, command, exe_ctx, result, internal_dict):
    help_message = 'Usage: jb_renderers_loaders_remove <loader_tag>'
    cmd = shlex.split(command)
    if len(cmd) < 1:
        result.SetError('Loader tag expected.\n{}'.format(help_message))
        return

    tag = cmd[0]
    type_viz_loader_remove(tag)


def _cmd_loaders_list(debugger, command, exe_ctx, result, internal_dict):
    lst = type_viz_loader_get_list()
    lst_view = {tag: func.__module__ + '.' + func.__name__ for tag, func in lst.items()}
    result.AppendMessage(str(lst_view))


def _cmd_load(debugger, command, exe_ctx, result, internal_dict):
    help_message = 'Usage: jb_renderers_load tag <loader_tag> <natvis_file_path>...'
    cmd = shlex.split(command)
    if len(cmd) < 1:
        result.SetError('Loader tag expected.\n{}'.format(help_message))
        return
    tag = cmd[0]
    try:
        loader = type_viz_loader_get(tag)
    except KeyError:
        result.SetError('Unknown loader tag {}'.format(tag))
        return

    file_paths = cmd[1:]
    for filepath in file_paths:
        try:
            entry = lldb_formatters_manager.register(filepath, loader)
            add_all_top_level_declarations(debugger, [entry])
        except TypeVizLoaderException as e:
            result.SetError('{}'.format(str(e)))
            return


def _cmd_remove(debugger, command, exe_ctx, result, internal_dict):
    help_message = 'Usage: jb_renderers_remove <vis_file_path>...'
    cmd = shlex.split(command)
    if len(cmd) < 1:
        result.SetError('At least one file expected.\n{}'.format(help_message))
        return

    remove_file_list(debugger, cmd)


def _cmd_reload(debugger, command, exe_ctx, result, internal_dict):
    help_message = 'Usage: jb_renderers_reload <vis_file_path>...'
    cmd = shlex.split(command)
    if len(cmd) < 1:
        result.SetError('At least one file expected.\n{}'.format(help_message))
        return

    reload_file_list(debugger, cmd)


def _cmd_remove_all(debugger, command, exe_ctx, result, internal_dict):
    remove_all(debugger)


def _cmd_reload_all(debugger, command, exe_ctx, result, internal_dict):
    reload_all(debugger)


def _cmd_list_all(debugger, command, exe_ctx, result, internal_dict):
    result.AppendMessage("\n".join(get_all_registered_files()))


def _cmd_override_charset(debugger, command, exe_ctx, result, internal_dict):
    help_message = 'Usage: jb_renderers_override_charset <charset>'
    cmd = shlex.split(command)
    if len(cmd) != 1:
        result.SetError('Charset name is expected.\n{}'.format(help_message))
        return

    override_locale(cmd[0])


def _cmd_set_markup(debugger, command, exe_ctx, result, internal_dict):
    help_message = 'Usage: jb_renderers_set_markup <value>'
    cmd = shlex.split(command)
    if len(cmd) != 1:
        result.SetError('Boolean value is expected.\n{}'.format(help_message))
        return

    try:
        enable = bool(distutils.util.strtobool(cmd[0]))
    except Exception as e:
        result.SetError('Boolean value is expected.\n{}'.format(help_message))
        return

    enable_disable_formatting(enable)


def _cmd_set_global_hex(debugger, command, exe_ctx, result, internal_dict):
    help_message = 'Usage: jb_renderers_set_global_hex <value> <value>'
    cmd = shlex.split(command)
    if len(cmd) != 2:
        result.SetError('Two boolean values are expected.\n{}'.format(help_message))
        return

    try:
        hex_enable = bool(distutils.util.strtobool(cmd[0]))
        hex_show_both = bool(distutils.util.strtobool(cmd[1]))
    except Exception as e:
        result.SetError('Boolean value is expected.\n{}'.format(help_message))
        return

    set_global_hex(hex_enable)
    set_global_hex_show_both(hex_show_both)


def remove_all(debugger):
    files = lldb_formatters_manager.get_all_registered_files()
    remove_file_list(debugger, files)


def reload_all(debugger):
    files = lldb_formatters_manager.get_all_registered_files()
    reload_file_list(debugger, files)


def get_all_registered_files():
    return lldb_formatters_manager.get_all_registered_files()


def remove_file_list(debugger, files):
    for filepath in files:
        lldb_formatters_manager.unregister(filepath)
    LLDBRemoveAllTopLevelLazyDeclarations(debugger)
    add_all_top_level_declarations(debugger, lldb_formatters_manager.formatter_entries.values())


def reload_file_list(debugger, files):
    for filepath in files:
        lldb_formatters_manager.reload(filepath)
    LLDBRemoveAllTopLevelLazyDeclarations(debugger)
    add_all_top_level_declarations(debugger, lldb_formatters_manager.formatter_entries.values())


def add_all_top_level_declarations(debugger: lldb.SBDebugger, entries: Iterable[FormattersManager.FormatterEntry]):
    for entry in entries:
        top_level_methods: List[SyntheticMethodDefinition] = entry.storage.get_top_level_methods()
        for top_level_method_definition in top_level_methods:
            if top_level_method_definition.name_uses_regex:
                error: lldb.SBError = LLDBAddTopLevelLazyDeclarationByRegex(
                    debugger,
                    top_level_method_definition.full_name,
                    top_level_method_definition.body_substitution,
                    eLanguageTypeC_plus_plus_14
                )
                if not error.Success():
                    log(f"Can't add lazy declarations by regex for '{top_level_method_definition.full_name}': "
                        f"{error.description}")
            else:
                error: lldb.SBError = LLDBAddTopLevelLazyDeclaration(
                    debugger,
                    top_level_method_definition.full_name,
                    top_level_method_definition.body_substitution,
                    eLanguageTypeC_plus_plus_14
                )
                if not error.Success():
                    log(f"Can't add lazy declarations for '{top_level_method_definition.full_name}': "
                        f"{error.description}")


def declarative_summary(val: lldb.SBValue, _):
    try:
        update_value_dynamic_state(val)
        val_non_synth = val.GetNonSyntheticValue()
        target = val_non_synth.GetTarget()
        is64bit: bool = target.GetAddressByteSize() == 8
        set_max_string_length(get_max_string_summary_length(target.GetDebugger()))
        stream_type = is_enabled_formatting() and FormattedStream or Stream
        stream: Stream = stream_type(is64bit, get_recursion_level())
        stream.output_object(val_non_synth)
        return str(stream)

    except IgnoreSynthProvider:
        return ''
    except:
        if not get_suppress_errors():
            raise
        return ''


class DeclarativeSynthProvider(object):
    """
    Implementation of SyntheticChildrenProvider from LLDB: https://lldb.llvm.org/use/variable.html#synthetic-children
    """

    def __init__(self, val: lldb.SBValue, _):
        """
        This call should initialize the Python object using val as the variable to provide synthetic children for.
        """
        update_value_dynamic_state(val)
        self.val_non_synth: lldb.SBValue = val.GetNonSyntheticValue()
        self.children_provider: Optional[AbstractChildrenProvider] = None

    def num_children(self, max_children: int) -> int:
        """
        This call should return the number of children that you want your object to have.

        :param max_children: The max_children argument indicates the maximum number of children that lldb is interested in (at this moment).
        If the computation of the number of children is expensive (for example, requires traversing a linked list to determine its size)
        your implementation may return `max_children` rather than the actual number. If the computation is cheap (e.g., the number is stored
        as a field of the object), then you can always return the true number of children (that is, ignore the `max_children` argument).
        """
        if not self.children_provider:
            self._create_children_provider()
        else:
            self.children_provider.try_update_size(self.val_non_synth)
        return self.children_provider.num_children()

    def get_child_index(self, name: str) -> int:
        """
        This call should return the index of the synthetic child whose name is given as argument.
        """
        # Ensure that `children_provider` is created because there is API which can call this method without prior call to `num_children`:
        # GetChildMemberWithName, GetIndexOfChildWithName
        if not self.children_provider:
            self._create_children_provider()
        return self.children_provider.get_child_index(name)

    def get_child_at_index(self, index: int) -> lldb.SBValue:
        """
        This call should return a new LLDB SBValue object representing the child at the index given as argument.
        """
        # Ensure that `children_provider` is created because there is API which can call this method without prior call to `num_children`:
        # GetChildMemberWithName, GetChildAtIndex
        if not self.children_provider:
            self._create_children_provider()
        return self.children_provider.get_child_at_index(index)

    # noinspection PyMethodMayBeStatic
    def update(self) -> bool:
        """
        This call should be used to update the internal state of this Python object whenever the state of the variables in LLDB changes.
        Also, this method is invoked before any other method in the interface.

        :return: If `False` is returned, then whenever the process reaches a new stop, this method will be invoked again to generate
        an updated list of the children for a given variable. Otherwise, if `True` is returned, then the value is cached and this method
        won’t be called again, effectively freezing the state of the value in subsequent stops.
        Beware that returning `True` incorrectly could show misleading information to the user.

        P.S.
        In the LLDB code there is following comment which actually contradicts to the official documentation:
            // this function is assumed to always succeed and it if fails, the front-end
            // should know to deal with it in the correct way (most probably, by refusing
            // to return any children) the return value of Update() should actually be
            // interpreted as "ValueObjectSyntheticFilter cache is good/bad" if =true,
            // ValueObjectSyntheticFilter is allowed to use the children it fetched
            // previously and cached if =false, ValueObjectSyntheticFilter must throw
            // away its cache, and query again for children
        While tests will continue work correctly if returning `True` here, it doesn't improve their execution time.
        """
        # We do not create `children_provider` here because it might be not needed later so why waste CPU time.
        # Also, we do not call `children_provider.try_update_size` because this `update` is called once for each child!
        # We rely on the fact that before enumerating children LLDB will call `num_children` and we call `try_update_size` from there.
        return False

    def has_children(self) -> bool:
        """
        This call should return `True` if this object might have children, and `False` if this object can be guaranteed to have no children.
        """
        return self.val_non_synth.MightHaveChildren()

    # def get_value(self) -> lldb.SBValue:
    #     """
    #     This call can return an `SBValue` to be presented as the value of the synthetic value under consideration.
    #     The `SBValue` you return here will most likely be a numeric type (int, float, …) as its value bytes
    #     will be used as-if they were the value of the root SBValue proper.
    #     """
    #     pass

    def _create_children_provider(self) -> None:
        try:
            log("Retrieving children of value named '{}'...", self.val_non_synth.GetName())

            format_spec = self.val_non_synth.GetFormat()
            provider = get_viz_descriptor_provider()
            vis_descriptor = provider.get_matched_visualizers(self.val_non_synth.GetType(), format_spec)
            if vis_descriptor:
                self.children_provider = vis_descriptor.prepare_children(self.val_non_synth)

        except IgnoreSynthProvider:
            pass
        except Exception as e:
            # some unexpected error happened
            if not get_suppress_errors():
                log("{}", traceback.format_exc())

        if not self.children_provider:
            self.children_provider = StructChildrenProvider(self.val_non_synth)


class VizDescriptorProvider(AbstractVizDescriptorProvider):
    def __init__(self):
        self.type_to_visualizer_cache = {}

    def get_matched_visualizers(self, value_type: lldb.SBType, format_spec: int) -> AbstractVisDescriptor:
        basic_specs = format_spec & eFormatBasicSpecsMask
        natvis_enabled = not (format_spec & eFormatRawView)
        cache_key = (value_type.GetName(), basic_specs, natvis_enabled)
        descriptor = self.type_to_visualizer_cache.get(cache_key, None)
        if descriptor is not None:
            return descriptor

        descriptor = _try_get_matched_visualizers(value_type, natvis_enabled, basic_specs)
        self.type_to_visualizer_cache[cache_key] = descriptor

        return descriptor


def _get_matched_type_visualizers(type_name_template, only_inherited=False):
    result = []
    if only_inherited:
        for type_viz_storage in lldb_formatters_manager.get_all_type_viz():
            result.extend(
                [name_match_pair for name_match_pair in type_viz_storage.get_matched_types(type_name_template) if
                 name_match_pair[0].is_inheritable])
    else:
        for type_viz_storage in lldb_formatters_manager.get_all_type_viz():
            result.extend(
                [name_match_pair for name_match_pair in type_viz_storage.get_matched_types(type_name_template)])
    return result


def _try_find_matched_natvis_visualizer_for_base(value_type: lldb.SBType) -> Optional[AbstractVisDescriptor]:
    for index in range(value_type.GetNumberOfDirectBaseClasses()):
        base_type = value_type.GetDirectBaseClassAtIndex(index).GetType()
        base_type_name = base_type.GetName()
        try:
            base_type_name_template = parse_type_name_template(base_type_name)
        except Exception as e:
            log('Parsing typename {} failed: {}', base_type_name, e)
            raise

        viz_candidates = _get_matched_type_visualizers(base_type_name_template, True)
        if viz_candidates:
            return NatVisDescriptor(viz_candidates, base_type_name_template)

        deep_base = _try_find_matched_natvis_visualizer_for_base(base_type)
        if deep_base is not None:
            return deep_base

    return None


def _try_get_matched_visualizers(value_type: lldb.SBType, natvis_enabled: bool, basic_fmt_spec: int) -> Optional[AbstractVisDescriptor]:
    value_type: lldb.SBType = value_type.GetUnqualifiedType()

    if natvis_enabled:
        value_type_name = CppParser.remove_type_class_specifier(value_type.GetName())
        log("Trying to find natvis visualizer for type: '{}'...", value_type_name)
        try:
            type_name_template = parse_type_name_template(value_type_name)
        except Exception as e:
            log('Parsing typename {} failed: {}', value_type_name, e)
            raise
        viz_candidates = _get_matched_type_visualizers(type_name_template)
        if viz_candidates:
            log("Found natvis visualizer for type: '{}'", value_type_name)
            return NatVisDescriptor(viz_candidates, type_name_template)

    return _try_get_matched_builtin_visualizer(value_type, natvis_enabled, basic_fmt_spec)


def _try_get_matched_builtin_visualizer(value_type: lldb.SBType, natvis_enabled: bool, basic_fmt_spec: int):
    value_type_name = value_type.GetName()
    log("Trying to find builtin visualizer for type: '{}'", value_type_name)

    type_class = value_type.GetTypeClass()
    if type_class == lldb.eTypeClassTypedef:
        value_typedef_type = value_type.GetTypedefedType()
        value_typedef_type_name = value_typedef_type.GetName()
        log("Type '{}' is typedef to type '{}'", value_type_name, value_typedef_type_name)
        if value_typedef_type_name != value_type_name:
            return _try_get_matched_visualizers(value_typedef_type, natvis_enabled, basic_fmt_spec)

    if type_class == lldb.eTypeClassBuiltin:
        str_presentation_info = FMT_STRING_SET_ALL.get(basic_fmt_spec)
        # When a format specifier is used, VS implicitly converts some integer types to a string pointer
        if str_presentation_info is not None and CharArrayOrPointerVisDescriptor.can_type_be_used_as_char_pointer(value_type):
            return CharArrayOrPointerVisDescriptor(str_presentation_info, False, None)

        char_presentation_info = CharVisDescriptor.char_types.get(value_type_name)
        if char_presentation_info is not None:
            return CharVisDescriptor(char_presentation_info)
        if value_type_name in NumberVisDescriptor.numeric_types:
            return NumberVisDescriptor(value_type_name)

    if type_class == lldb.eTypeClassArray:
        array_element_type: SBType = value_type.GetArrayElementType()
        array_element_type_name = array_element_type.GetName()
        str_presentation_info = CharVisDescriptor.char_types.get(array_element_type_name)
        if str_presentation_info is not None:
            array_size = value_type.size // array_element_type.GetByteSize()
            return CharArrayOrPointerVisDescriptor(str_presentation_info, True, array_size)
        return GenericArrayVisDescriptor()

    if type_class == lldb.eTypeClassPointer:
        pointee_type: SBType = value_type.GetPointeeType()
        pointee_type_name = pointee_type.GetName()
        str_presentation_info = CharVisDescriptor.char_types.get(pointee_type_name)
        if str_presentation_info is not None:
            return CharArrayOrPointerVisDescriptor(str_presentation_info, False, None)
        # TODO: check pointer on typedef
        pointee_type_class = pointee_type.GetTypeClass()
        pointee_expands = pointee_type_class in {lldb.eTypeClassStruct,
                                                 lldb.eTypeClassClass,
                                                 lldb.eTypeClassUnion}
        # this is a hack
        # proper solution would be to clone stream inside visualiser and fallback if pointee summary is empty
        pointee_has_empty_description = pointee_type_name == 'void' or pointee_type_class == lldb.eTypeClassFunction
        return GenericPointerVisDescriptor(pointee_expands, pointee_has_empty_description)

    if type_class == lldb.eTypeClassReference:
        return GenericReferenceVisDescriptor()

    if type_class == lldb.eTypeClassStruct or type_class == lldb.eTypeClassClass or type_class == lldb.eTypeClassUnion:
        if natvis_enabled:
            natvis = _try_find_matched_natvis_visualizer_for_base(value_type)
            if natvis is not None:
                return natvis
        lambda_name = _try_extract_lambda_type_name(value_type_name)
        if lambda_name is not None:
            return LambdaVisDescriptor(value_type, lambda_name)
        return StructVisDescriptor(value_type)

    # No matched builtin vis descriptor found
    return None


def _try_extract_lambda_type_name(type_name: str) -> Optional[str]:
    idx = type_name.rfind('<lambda_')
    if idx == -1:
        return None
    if type_name[-1] != '>':
        return None
    if idx == 0:
        return type_name
    extracted_name = type_name[idx + len("<lambda_"):-1]
    if not extracted_name.isalnum():
        return None
    return type_name[idx:]
