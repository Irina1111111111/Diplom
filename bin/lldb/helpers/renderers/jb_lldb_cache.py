from __future__ import annotations

from typing import Any, Hashable

import lldb
from renderers.jb_lldb_logging import log


class LLDBCache:
    """
    The cache may keep values associated with a specific process. The keys can be any Hashable objects.
    The cache lazily cleans itself on specific debugger target events, for example on modules or symbols loading.
    """
    class _CacheForProcess:
        def __init__(self, process: lldb.SBProcess, name: str, clear_on_target_events: int):
            self._name = f"{name}.Process.{process.GetUniqueID()}"
            self._listener = lldb.SBListener(self._name)
            self._listener.StartListeningForEvents(process.GetTarget().GetBroadcaster(), clear_on_target_events)
            self._cache = {}

        def _sync_cache(self):
            has_any_event = False
            event = lldb.SBEvent()
            while self._listener.GetNextEvent(event):
                has_any_event = True

            if has_any_event:
                log(f"[{self._name}]: Got an event, clear the cache")
                self._cache = {}

        def get(self, key: Hashable) -> Any | None:
            self._sync_cache()
            return self._cache.get(key, None)

        def set(self, key: Hashable, value: Any):
            self._cache[key] = value

    def __init__(self, name: str, clear_on_target_events: int):
        self._name = name
        self._clear_on_target_events = clear_on_target_events
        self._caches_for_process = {}

    def _get_cache_for_process(self, process: lldb.SBProcess) -> _CacheForProcess:
        process_id = process.GetUniqueID()
        cache_for_process = self._caches_for_process.get(process_id, None)
        if cache_for_process is None:
            cache_for_process = self._CacheForProcess(process, self._name, self._clear_on_target_events)
            self._caches_for_process[process_id] = cache_for_process
        return cache_for_process

    def get_for_process(self, process: lldb.SBProcess, key: Hashable) -> Any | None:
        if not process.IsValid():
            return None
        return self._get_cache_for_process(process).get(key)

    def set_for_process(self, process: lldb.SBProcess, key: Hashable, value: Any):
        if process.IsValid():
            self._get_cache_for_process(process).set(key, value)
