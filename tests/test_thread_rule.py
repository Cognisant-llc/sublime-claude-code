"""Static guard for the panel's thread rule (see adapters/activity_panel.py
``_lock``): code that runs on the watcher threads must never reference the
``sublime`` API, and the logger must not either. A violation is the exact
deadlock that froze Sublime in 0.3.5, so it is checked at test time rather
than trusted to review."""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Functions invoked from fswatch's reader/flush threads (see Watcher docstring)
# or from anything they call while holding ``_lock``.
BACKGROUND_FUNCS = {"_batch_filter", "_on_fs_change", "_on_watch_error",
                    "_should_ignore", "_burst_key", "_log", "_held"}


def _functions(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _sublime_refs(node):
    return [n.lineno for n in ast.walk(node)
            if isinstance(n, ast.Name) and n.id == "sublime"]


def test_watcher_thread_code_never_touches_the_api():
    funcs = _functions(os.path.join(ROOT, "adapters", "activity_panel.py"))
    missing = BACKGROUND_FUNCS - set(funcs)
    assert not missing, f"renamed? {missing}"
    bad = {name: _sublime_refs(funcs[name]) for name in BACKGROUND_FUNCS}
    assert not any(bad.values()), f"sublime.* used on a watcher thread: {bad}"


def test_bridge_logger_never_touches_the_api():
    funcs = _functions(os.path.join(ROOT, "adapters", "sublime_bridge.py"))
    assert not _sublime_refs(funcs["log"])


def test_main_thread_takes_the_lock_with_a_timeout():
    """Only startup/shutdown may block on ``_lock`` from the main thread;
    timers and renders must go through ``_held``."""
    path = os.path.join(ROOT, "adapters", "activity_panel.py")
    funcs = _functions(path)
    blocking = set()
    for name, node in funcs.items():
        for n in ast.walk(node):
            if (isinstance(n, ast.With) and any(
                    isinstance(i.context_expr, ast.Name) and i.context_expr.id == "_lock"
                    for i in n.items)):
                blocking.add(name)
    assert blocking <= {"start", "stop"} | BACKGROUND_FUNCS, blocking
