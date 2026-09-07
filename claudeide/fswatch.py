"""Recursive directory watcher (Windows ``ReadDirectoryChangesW`` via ctypes).

One blocking reader thread per root; events are coalesced per path with a
short quiet period so a file written in many chunks (a video render, a
streamed log) surfaces once. Pure Python 3.8 stdlib, never imports
``sublime``. On non-Windows platforms ``available()`` is False and the
panel falls back to hook records only.
"""

import os
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

ACTION_NAMES = {1: "added", 2: "removed", 3: "modified", 4: "renamed_from", 5: "renamed_to"}

_WIN = sys.platform == "win32"

if _WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_SHARE_ALL = 0x0001 | 0x0002 | 0x0004
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _NOTIFY_FLAGS = (0x0001  # FILE_NAME
                     | 0x0002  # DIR_NAME
                     | 0x0008  # SIZE
                     | 0x0010  # LAST_WRITE
                     | 0x0040)  # CREATION
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.ReadDirectoryChangesW.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID, wintypes.LPVOID,
    ]
    _k32.ReadDirectoryChangesW.restype = wintypes.BOOL
    _k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    _k32.CancelIoEx.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


def available() -> bool:
    return _WIN


def parse_notify_buffer(buf: bytes) -> List[Tuple[int, str]]:
    """Decode a FILE_NOTIFY_INFORMATION chain into ``[(action, relpath)]``.
    Pure function (testable without the OS)."""
    out = []  # type: List[Tuple[int, str]]
    off = 0
    n = len(buf)
    while off + 12 <= n:
        next_off = int.from_bytes(buf[off:off + 4], "little")
        action = int.from_bytes(buf[off + 4:off + 8], "little")
        name_len = int.from_bytes(buf[off + 8:off + 12], "little")
        name = buf[off + 12:off + 12 + name_len].decode("utf-16-le", "replace")
        out.append((action, name))
        if next_off == 0:
            break
        off += next_off
    return out


class _RootReader(threading.Thread):
    def __init__(self, root: str, on_events: Callable[[str, List[Tuple[int, str]]], None],
                 on_error: Callable[[str, str], None]) -> None:
        super().__init__(name="claude-fswatch:" + os.path.basename(root), daemon=True)
        self.root = root
        self._on_events = on_events
        self._on_error = on_error
        self._handle = None
        self._stop = threading.Event()

    def run(self) -> None:  # pragma: no cover - needs the OS
        handle = _k32.CreateFileW(self.root, _FILE_LIST_DIRECTORY, _FILE_SHARE_ALL, None,
                                  _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None)
        if handle == _INVALID_HANDLE or handle is None:
            self._on_error(self.root, f"CreateFileW failed ({ctypes.get_last_error()})")
            return
        self._handle = handle
        buf = ctypes.create_string_buffer(256 * 1024)
        returned = wintypes.DWORD(0)
        try:
            while not self._stop.is_set():
                ok = _k32.ReadDirectoryChangesW(handle, buf, len(buf), True, _NOTIFY_FLAGS,
                                                ctypes.byref(returned), None, None)
                if self._stop.is_set():
                    break
                if not ok:
                    err = ctypes.get_last_error()
                    if err == 995:  # ERROR_OPERATION_ABORTED (CancelIoEx)
                        break
                    self._on_error(self.root, f"ReadDirectoryChangesW failed ({err})")
                    time.sleep(1.0)
                    continue
                if returned.value == 0:
                    # buffer overflow: too many changes at once; report and go on
                    self._on_error(self.root, "overflow")
                    continue
                events = parse_notify_buffer(buf.raw[: returned.value])
                self._on_events(self.root, events)
        finally:
            _k32.CloseHandle(handle)
            self._handle = None

    def stop(self) -> None:
        self._stop.set()
        h = self._handle
        if h is not None:
            _k32.CancelIoEx(h, None)


class Watcher:
    """Watch a changing set of roots; deliver coalesced ``(path, ts, action)``.

    ``callback(path, ts, action)`` runs on the watcher's own timer thread.
    ``should_ignore(root, relpath)`` lets the caller prune noisy subtrees
    before any coalescing work is done. ``batch_filter(events)`` sees each
    flush batch ``[(path, ts, action)]`` whole and returns the events to
    deliver — the hook for dropping storms (a git checkout rewriting hundreds
    of files) that only a batch, not a single event, can reveal.
    """

    def __init__(self, callback: Callable[[str, float, str], None],
                 should_ignore: Optional[Callable[[str, str], bool]] = None,
                 quiet_seconds: float = 0.8,
                 on_error: Optional[Callable[[str, str], None]] = None,
                 batch_filter: Optional[Callable[[List[Tuple[str, float, str]]],
                                                 List[Tuple[str, float, str]]]] = None) -> None:
        self._cb = callback
        self._ignore = should_ignore or (lambda root, rel: False)
        self._quiet = quiet_seconds
        self._on_error = on_error or (lambda root, msg: None)
        self._batch_filter = batch_filter
        self._readers = {}  # type: Dict[str, _RootReader]
        self._pending = {}  # type: Dict[str, Tuple[float, str, str]]   norm -> (ts, path, action)
        self._lock = threading.Lock()
        self._flusher = None  # type: Optional[threading.Thread]
        self._stop = threading.Event()

    def roots(self) -> List[str]:
        return sorted(self._readers)

    def set_roots(self, roots: List[str]) -> None:
        wanted = {os.path.normpath(r) for r in roots if os.path.isdir(r)}
        # never watch a root nested in another watched root
        for r in sorted(wanted, key=len):
            for other in list(wanted):
                if other != r and other.lower().startswith(r.lower() + os.sep):
                    wanted.discard(other)
        current = set(self._readers)
        for r in current - wanted:
            self._readers.pop(r).stop()
        for r in wanted - current:
            if not available():
                continue
            reader = _RootReader(r, self._on_events, self._on_error)
            self._readers[r] = reader
            reader.start()
        if self._flusher is None and available():
            self._flusher = threading.Thread(target=self._flush_loop, name="claude-fswatch:flush",
                                             daemon=True)
            self._flusher.start()

    def stop(self) -> None:
        self._stop.set()
        for reader in self._readers.values():
            reader.stop()
        self._readers.clear()

    # -- internals --

    def _on_events(self, root: str, events: List[Tuple[int, str]]) -> None:
        now = time.time()
        with self._lock:
            for action, rel in events:
                if self._ignore(root, rel):
                    continue
                path = os.path.join(root, rel)
                name = ACTION_NAMES.get(action, "modified")
                if name == "renamed_from":
                    name = "removed"
                elif name == "renamed_to":
                    name = "added"
                key = os.path.normcase(path)
                prev = self._pending.get(key)
                if prev and prev[2] == "removed" and name != "removed":
                    name = "modified"
                self._pending[key] = (now, path, name)

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.3)
            now = time.time()
            due = []
            with self._lock:
                for key, (ts, path, action) in list(self._pending.items()):
                    if now - ts >= self._quiet:
                        due.append((path, ts, action))
                        del self._pending[key]
            if due and self._batch_filter is not None:
                try:
                    due = self._batch_filter(due)
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    self._on_error("", f"batch filter failed: {exc}")
            for path, ts, action in due:
                if action != "removed":
                    if os.path.isdir(path):
                        continue  # directories themselves are not listed
                    if not os.path.exists(path):
                        action = "removed"
                try:
                    self._cb(path, ts, action)
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    self._on_error(path, f"callback failed: {exc}")
