import lldb


def LLDBAddTopLevelLazyDeclaration(debugger: lldb.SBDebugger, *args) -> lldb.SBError:
    if getattr(debugger, "AddTopLevelLazyDeclaration", None) is None:
        return lldb.SBError()
    return debugger.AddTopLevelLazyDeclaration(*args)


def LLDBAddTopLevelLazyDeclarationByRegex(debugger: lldb.SBDebugger, *args) -> lldb.SBError:
    if getattr(debugger, "AddTopLevelLazyDeclarationByRegex", None) is None:
        return lldb.SBError()
    return debugger.AddTopLevelLazyDeclarationByRegex(*args)


def LLDBRemoveAllTopLevelLazyDeclarations(debugger: lldb.SBDebugger):
    if getattr(debugger, "RemoveAllTopLevelLazyDeclarations", None) is None:
        return
    debugger.RemoveAllTopLevelLazyDeclarations()
