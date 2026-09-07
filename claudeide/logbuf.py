"""Console logging that is safe from any thread.

Inside plugin_host every ``sublime.*`` call — ``load_settings`` included —
is a round trip to the sublime_text process. While sublime_text is waiting
for a *main-thread* callback to return (a timer, an event listener), it does
not answer calls coming from the plugin's other threads. So a background
thread that holds a lock and then touches the API deadlocks the editor as
soon as the main thread blocks on that same lock (seen live: 0.3.5's burst
gate logged under the panel lock while ``_tick`` waited for it; Sublime
stopped responding and had to be killed).

Rule: background threads never call the API, not even to read the ``debug``
flag. They queue their message here; the main thread prints the queue at its
next tick. The flag itself is cached by the main thread.
"""

import threading
from collections import deque

_MAX_BUFFERED = 500

_main = None  # main thread, set by remember_main_thread()
_enabled = False
_buf = deque(maxlen=_MAX_BUFFERED)  # str lines
_buf_lock = threading.Lock()


def remember_main_thread() -> None:
    global _main
    _main = threading.current_thread()


def on_main_thread() -> bool:
    return _main is None or threading.current_thread() is _main


def set_enabled(flag: bool) -> None:
    """Cache the ``debug`` setting (call from the main thread, on change)."""
    global _enabled
    _enabled = bool(flag)


def enabled() -> bool:
    return _enabled


def log(msg: str) -> None:
    """Print ``msg`` when debugging is on. Off the main thread the line is
    buffered instead of printed: printing goes through the API too."""
    if not _enabled:
        return
    if on_main_thread():
        drain()
        print(msg)
        return
    with _buf_lock:
        _buf.append(msg)


def drain() -> int:
    """Main thread: print everything background threads queued. Returns the
    number of lines printed."""
    with _buf_lock:
        if not _buf:
            return 0
        lines = list(_buf)
        _buf.clear()
    for line in lines:
        print(line)
    return len(lines)


def pending() -> int:
    with _buf_lock:
        return len(_buf)
