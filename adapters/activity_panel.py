"""Recent Activity panel — the Sublime side.

A read-only scratch view (left-hand group by default) listing files that
Claude sessions changed recently, grouped ``project → session → file``.
Data comes from ``claudeide.activity`` (hook log + filesystem watcher);
this module owns the view, the timers, and the click/key handling.
"""

import json
import os
import threading
import time

import sublime

from ..claudeide import activity as A
from ..claudeide import fswatch

SETTINGS_FILE = "Claude Code IDE.sublime-settings"
PANEL_SETTING = "claude_activity_panel"
STATE_SETTING = "claude_activity_state"
STATUS_KEY = "claude_activity"
SYNTAX = "Packages/Claude Code IDE/Recent Activity.sublime-syntax"
PANEL_NAME = "◔ Recent Activity"
PANEL_LAYOUT = {"cols": [0.0, 0.2, 1.0], "rows": [0.0, 1.0],
                "cells": [[0, 0, 1, 1], [1, 0, 2, 1]]}

DEFAULTS = {
    "enabled": True,
    "auto_open": True,
    "group": 0,
    "min_width_chars": 34,
    "window_hours": 24,
    "show_code": False,
    "scope": "all",
    "hide_projects": [],
    "max_files_per_session": 20,
    "fit_to_view": True,
    "poll_ms": 2000,
    "keep_days": 7,
    "prune": A.DEFAULT_PRUNE,
    "font_size": None,
}

# A hard ceiling on each log so a pathological burst can never grow it without
# bound, on top of the keep_days age limit.
MAX_LOG_RECORDS = 20000
# Compact the logs in the background this often (seconds), not only at startup,
# so a Sublime that stays open for days does not let them grow unbounded.
COMPACT_EVERY = 1800.0

_lock = threading.RLock()
_model = None  # type: A.ActivityModel
_watcher = None  # type: fswatch.Watcher
_gate = None  # type: A.BurstGate
_hook_offset = 0
_running = False
_dirty = False
_render_scheduled = False
_last_error = ""
_last_compact = 0.0


# ---------- settings / paths ----------


def conf():
    base = dict(DEFAULTS)
    user = sublime.load_settings(SETTINGS_FILE).get("activity_panel", {}) or {}
    base.update(user)
    return base


def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def hook_log_path():
    return os.path.join(claude_home(), "logs", "file-activity.jsonl")


def fs_log_path():
    return os.path.join(claude_home(), "logs", "file-activity-fs.jsonl")


def sessions_dir():
    return os.path.join(claude_home(), "sessions")


def hidden_path():
    """Projects hidden by hand (the `h` key) persist here, so a hide survives
    restarts without editing settings by hand."""
    return os.path.join(claude_home(), "logs", "activity-hidden.json")


def load_manual_hidden():
    try:
        with open(hidden_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        got = data.get("hidden", []) if isinstance(data, dict) else data
        return [str(x) for x in got if x]
    except (OSError, ValueError):
        return []


def save_manual_hidden(paths):
    seen, out = set(), []
    for p in paths:
        n = A.norm(p)
        if n not in seen:
            seen.add(n)
            out.append(p)
    try:
        os.makedirs(os.path.dirname(hidden_path()), exist_ok=True)
        with open(hidden_path(), "w", encoding="utf-8") as fh:
            json.dump({"hidden": out}, fh, ensure_ascii=False, indent=1)
    except OSError as exc:
        _log(f"hidden save failed: {exc}")


def all_hidden():
    """Union of the `hide_projects` setting (system/manual, editable) and the
    `h`-key persisted list."""
    return list(conf().get("hide_projects") or []) + load_manual_hidden()


def _log(msg):
    if sublime.load_settings(SETTINGS_FILE).get("debug", False):
        print("[ClaudeCodeIDE] activity: " + msg)


# ---------- lifecycle ----------


def start():
    global _model, _watcher, _gate, _hook_offset, _running
    c = conf()
    if not c["enabled"]:
        return
    with _lock:
        if _running:
            return
        _running = True
        _model = A.ActivityModel(prune=c["prune"])
        _gate = A.BurstGate()
        keep = float(c["keep_days"]) * 86400.0
        _compact_hook_log(keep)
        _model.set_sessions(A.load_sessions(sessions_dir()))
        _hook_offset = 0
        _ingest_hook_log()
        _model.rebuild_roots()  # anchors from the hook log before fs backfill
        _compact_fs_log(keep)  # after the roots exist: storms are keyed by project
        _backfill_fs_log()
        _model.reattribute()
        _watcher = fswatch.Watcher(_on_fs_change, should_ignore=_should_ignore,
                                   on_error=_on_watch_error, batch_filter=_batch_filter)
        _watcher.set_roots(_model.roots.watch_dirs())
    sublime.set_timeout(_tick, int(c["poll_ms"]))
    if c["auto_open"]:
        sublime.set_timeout(lambda: _ensure_panels(create=True), 300)
    else:
        sublime.set_timeout(lambda: _ensure_panels(create=False), 300)


def stop():
    global _running, _watcher
    with _lock:
        _running = False
        if _watcher is not None:
            _watcher.stop()
            _watcher = None


def is_running():
    return _running


# ---------- data ingestion ----------


def _ingest_hook_log():
    """Tail the hook log; returns True when new records were read."""
    global _hook_offset
    records, _hook_offset = A.read_log_tail(hook_log_path(), _hook_offset)
    for rec in records:
        _model.ingest_hook(rec)
    return bool(records)


def _compact_logs(keep_seconds):
    """Bound both logs by age and count (periodic; see the two halves)."""
    _compact_hook_log(keep_seconds)
    _compact_fs_log(keep_seconds)


def _compact_hook_log(keep_seconds):
    """Runs under _lock; the hook tail offset is reset to the compacted size so
    nothing is re-ingested."""
    global _hook_offset, _last_compact
    p = hook_log_path()
    if os.path.exists(p):
        try:
            A.compact_log(p, keep_seconds, max_records=MAX_LOG_RECORDS)
        except OSError as exc:
            _log(f"compact failed: {exc}")
    try:
        _hook_offset = os.path.getsize(p)
    except OSError:
        _hook_offset = 0
    _last_compact = time.time()


def _burst_key(path):
    return A.burst_key(path, _model.roots)


def _fs_keep_pred():
    """Which fs-log records survive a compaction or a backfill: default-view
    records (A.is_fs_loggable — what physically removes legacy system churn)
    that are not part of a storm (a git checkout that rewrote hundreds of
    documents in one time bucket; see A.storm_buckets)."""
    records, _ = A.read_log_tail(fs_log_path(), 0)
    storms = A.storm_buckets(records, _burst_key)

    def keep(rec):
        path = rec.get("path", "")
        if rec.get("ev") != "fs" or not path or not A.is_fs_loggable(path):
            return False
        return (_burst_key(path), int(float(rec.get("ts", 0)) // A.BURST_WINDOW)) not in storms
    return keep


def _compact_fs_log(keep_seconds):
    p = fs_log_path()
    if not os.path.exists(p):
        return
    try:
        A.compact_log(p, keep_seconds, max_records=MAX_LOG_RECORDS, keep_pred=_fs_keep_pred())
    except OSError as exc:
        _log(f"compact failed: {exc}")


def _backfill_fs_log():
    # roots come only from session anchors (rebuild_roots ran before this);
    # a change outside any anchor is picked up once its session is attributed
    records, _ = A.read_log_tail(fs_log_path(), 0)
    keep = _fs_keep_pred()
    for rec in records:
        if keep(rec):
            _model.ingest_fs(rec["path"], float(rec["ts"]), rec.get("action", "modified"))


def _batch_filter(due):
    """Watcher thread: drop a flush batch that is a storm for its project (a
    git checkout / rebase rewriting hundreds of files), per A.BurstGate."""
    global _dirty
    groups = {}
    with _lock:
        if not _running:
            return []
        for item in due:
            groups.setdefault(_burst_key(item[0]), []).append(item)
        now = time.time()
        kept = []
        for key, items in groups.items():
            if _gate.batch(key, len(items), now):
                kept.extend(items)
            else:
                _dirty = True  # the header shows the running total
                _log(f"burst: dropped {len(items)} events in {key}")
    return kept


def _should_ignore(root, rel):
    parts = rel.split(os.sep)
    return A.is_pruned(parts[:-1], _model.prune) or A.is_temp_name(parts[-1])


def _on_fs_change(path, ts, action):
    """Watcher thread: ingest into the live model, and persist to the fs log
    only default-view changes (see A.is_fs_loggable) so the file stays lean."""
    global _dirty
    with _lock:
        if not _running:
            return
        accepted = _model.ingest_fs(path, ts, action)
        if accepted:
            _dirty = True
            if action != "removed" and A.is_fs_loggable(path):
                try:
                    os.makedirs(os.path.dirname(fs_log_path()), exist_ok=True)
                    with open(fs_log_path(), "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"ts": round(ts, 3), "ev": "fs",
                                             "path": path, "action": action},
                                            ensure_ascii=False) + "\n")
                except OSError as exc:
                    _log(f"fs log write failed: {exc}")
    sublime.set_timeout(_schedule_render, 0)


def _on_watch_error(root, msg):
    global _last_error
    _last_error = f"{os.path.basename(root) or root}: {msg}"
    _log("watch error " + _last_error)


def _tick():
    global _dirty
    if not _running:
        return
    c = conf()
    changed = False
    with _lock:
        try:
            live = A.load_sessions(sessions_dir())
            prev = {(s.sid, s.status, s.name) for s in _model.sessions.values() if s.live}
            now = {(s.sid, s.status, s.name) for s in live.values()}
            if prev != now:
                changed = True
            _model.set_sessions(live)
            if _ingest_hook_log():
                _model.reattribute()
                changed = True
            _model.trim(float(c["keep_days"]) * 86400.0)
            if time.time() - _last_compact > COMPACT_EVERY:
                _compact_logs(float(c["keep_days"]) * 86400.0)
            wanted = _model.roots.watch_dirs()
            if _watcher is not None and sorted(wanted) != _watcher.roots():
                _watcher.set_roots(wanted)
        except Exception as exc:  # noqa: BLE001 - keep the timer alive
            _log(f"tick failed: {exc}")
        if _dirty:
            changed = True
            _dirty = False
    if not changed:
        # the user may have dragged the column border: re-flow to the new width
        for window in sublime.windows():
            view = find_panel(window)
            if view is None:
                continue
            if (view.settings().get("claude_activity_width") != _text_width(view)
                    or view.settings().get("claude_activity_lines") != _text_lines(view)):
                changed = True
                break
    if changed:
        _schedule_render()
    sublime.set_timeout(_tick, int(c["poll_ms"]))


# ---------- panel views ----------


def find_panel(window):
    for view in window.views():
        if view.settings().get(PANEL_SETTING):
            return view
    return None


def _panel_state(view):
    st = view.settings().get(STATE_SETTING) or {}
    return {
        "collapsed": list(st.get("collapsed", [])),
        "focus": list(st.get("focus", [])),
        "show_code": bool(st.get("show_code", conf()["show_code"])),
        "window_hours": float(st.get("window_hours", conf()["window_hours"])),
        "last_seen": float(st.get("last_seen", 0.0)),
    }


def _set_panel_state(view, **kw):
    st = _panel_state(view)
    st.update(kw)
    view.settings().set(STATE_SETTING, st)


def open_panel(window, focus=True):
    """Create (or reveal) the panel in the configured group."""
    view = find_panel(window)
    c = conf()
    if view is None:
        group = int(c["group"])
        if window.num_groups() == 1 and group == 0:
            window.set_layout(PANEL_LAYOUT)
        group = max(0, min(group, window.num_groups() - 1))
        active = window.active_view()
        view = window.new_file()
        view.set_name(PANEL_NAME)
        view.set_scratch(True)
        view.settings().set(PANEL_SETTING, True)
        _apply_view_settings(view)
        view.set_read_only(True)
        window.set_view_index(view, group, 0)
        _set_panel_state(view, last_seen=time.time())
        if not focus and active is not None:
            window.focus_view(active)
        # the viewport has no size until the layout settles
        sublime.set_timeout(lambda: (_fit_width(window, view), render_view(view)), 150)
    render_view(view)
    if focus:
        window.focus_view(view)
    return view


def _apply_view_settings(view):
    """Panel-only view settings (re-applied on every start so panels restored
    from a previous session pick up changes)."""
    s = view.settings()
    for key, val in (("word_wrap", True), ("wrap_width", 0), ("gutter", False),
                     ("line_numbers", False), ("draw_indent_guides", False),
                     ("scroll_past_end", False), ("highlight_line", True), ("rulers", []),
                     ("fold_buttons", False), ("draw_centered", False), ("spell_check", False),
                     ("translate_tabs_to_spaces", True), ("mini_diff", False),
                     ("show_git_status", False)):
        s.set(key, val)
    font = conf()["font_size"]
    if font:
        s.set("font_size", font)
    elif s.has("font_size"):
        s.erase("font_size")
    view.assign_syntax(SYNTAX)
    view.set_read_only(True)


def _fit_width(window, view):
    """Widen the panel's column so at least ``min_width_chars`` characters
    fit. Only the column boundary to the panel's right moves; columns
    further right shrink proportionally. Never wider than half the window."""
    min_chars = float(conf().get("min_width_chars") or 0)
    if min_chars <= 0 or not view.is_valid():
        return
    try:
        layout = window.get_layout()
        group = window.get_view_index(view)[0]
        cell = layout["cells"][group]
        cols = list(layout["cols"])
        left, right = cell[0], cell[2]
        if right - left != 1 or right >= len(cols):
            return
        frac = cols[right] - cols[left]
        width = view.viewport_extent()[0]
        if width <= 0 or frac <= 0:
            return
        adv = _char_advance(view)
        if adv <= 0:
            return
        total = width / frac
        need = min(0.5, (min_chars + 3) * adv / total)
        if frac >= need:
            return
        new_cols = cols[:]
        new_cols[right] = cols[left] + need
        span_old = 1.0 - cols[right]
        span_new = 1.0 - new_cols[right]
        for i in range(right + 1, len(cols)):
            if span_old > 0:
                new_cols[i] = new_cols[right] + (cols[i] - cols[right]) * (span_new / span_old)
        layout["cols"] = new_cols
        window.set_layout(layout)
    except Exception as exc:  # noqa: BLE001 - layout tweaks are best-effort
        _log(f"fit width failed: {exc}")


def _ensure_panels(create):
    for window in sublime.windows():
        view = find_panel(window)
        if view is not None:
            _apply_view_settings(view)
            _fit_width(window, view)
            render_view(view)
        elif create:
            open_panel(window, focus=False)


def _schedule_render():
    global _render_scheduled
    if _render_scheduled:
        return
    _render_scheduled = True
    sublime.set_timeout(_render_all, 250)


def _render_all():
    global _render_scheduled
    _render_scheduled = False
    for window in sublime.windows():
        view = find_panel(window)
        if view is not None:
            render_view(view)
        _update_status(window.active_view())


def _char_advance(view):
    """Layout-space width of one ASCII character, measured on the rendered
    text (``em_width`` is not in the same units as ``viewport_extent`` on
    scaled displays; ``text_to_layout`` is)."""
    try:
        if view.size() >= 2 and view.substr(sublime.Region(0, 2)).isascii():
            x0 = view.text_to_layout(0)[0]
            x1 = view.text_to_layout(1)[0]
            if x1 > x0:
                return x1 - x0
        return float(view.em_width())
    except Exception:  # noqa: BLE001
        return 0.0


def _text_width(view):
    try:
        adv = _char_advance(view)
        w = view.viewport_extent()[0]
        if adv > 0 and w > 0:
            return max(24, int(w / adv) - 3)
    except Exception:  # noqa: BLE001
        pass
    return 40


def _text_lines(view):
    """Rows that fit the viewport (the fit-to-view line budget), or None when
    the user turned fitting off or the view has no size yet."""
    if not conf().get("fit_to_view", True):
        return None
    try:
        h = view.viewport_extent()[1]
        lh = view.line_height()
        if h > 0 and lh > 0:
            return max(8, int(h // lh))
    except Exception:  # noqa: BLE001
        pass
    return None


def render_view(view):
    if _model is None or not view.is_valid():
        return
    st = _panel_state(view)
    c = conf()
    within = _scope_dirs(view.window())
    hidden = all_hidden()
    with _lock:
        tree = _model.tree(st["window_hours"] * 3600.0, show_code=st["show_code"],
                           within=within, hidden=hidden)
        live = sum(1 for s in _model.sessions.values() if s.live)
    hours = st["window_hours"]
    span = f"{hours:g}h" if hours < 48 else f"{hours / 24.0:g}d"
    burst = _gate.total_dropped() if _gate is not None else 0
    header = "{}  ·  {}{}{}{}".format(
        span, "all files" if st["show_code"] else "docs & media",
        f"  ·  {len(hidden)} hidden" if hidden else "",
        f"  ·  ⚡{burst} burst" if burst else "",
        "  ·  ⚠ " + _last_error if _last_error else "")
    width = _text_width(view)
    max_lines = _text_lines(view)
    text, targets = A.render(tree, st["collapsed"], st["last_seen"], width=width,
                             max_files=int(c["max_files_per_session"]), header=header,
                             max_lines=max_lines, focus=st["focus"])
    view.settings().set("claude_activity_width", width)
    view.settings().set("claude_activity_lines", max_lines)
    view.settings().set("claude_activity_targets",
                        {str(k): list(v) for k, v in targets.items()})
    if view.substr(sublime.Region(0, view.size())) == text:
        _set_status_text(view, A.summary(tree, live))
        return
    pos = view.viewport_position()
    sel = [(r.a, r.b) for r in view.sel()]
    view.set_read_only(False)
    view.run_command("claude_ide_replace_content", {"text": text})
    view.set_read_only(True)
    view.sel().clear()
    for a, b in sel:
        view.sel().add(sublime.Region(min(a, view.size()), min(b, view.size())))
    view.set_viewport_position(pos, False)
    _set_status_text(view, A.summary(tree, live))


def _scope_dirs(window):
    """``scope: "window"`` limits the panel to the window's folders."""
    if conf().get("scope") != "window" or window is None:
        return None
    folders = window.folders()
    return folders if folders else None


def _set_status_text(view, text):
    view.settings().set("claude_activity_summary", text)
    view.set_status(STATUS_KEY, text)


def _update_status(view):
    if view is None or _model is None:
        return
    window = view.window()
    panel = find_panel(window) if window else None
    if panel is None:
        view.erase_status(STATUS_KEY)
        return
    text = panel.settings().get("claude_activity_summary")
    if text:
        view.set_status(STATUS_KEY, text)


# ---------- interaction ----------


def target_at(view, point):
    row = view.rowcol(point)[0]
    targets = view.settings().get("claude_activity_targets") or {}
    t = targets.get(str(row))
    if not t:
        return None
    return tuple(t)


_last_activation = ("", 0.0)


def activate(view, point=None):
    """Enter / click: open the file, or fold a project header."""
    global _last_activation
    if point is None:
        sel = view.sel()
        if not sel:
            return
        point = sel[0].b
    t = target_at(view, point)
    if t is None:
        return
    kind, target = t
    # a double-click arrives as click + click-by-words: act once
    now = time.time()
    if _last_activation[0] == target and now - _last_activation[1] < 0.7:
        return
    _last_activation = (target, now)
    if kind == "pj-auto":
        # folded only to fit the view: a click focuses it (expands it fully)
        st = _panel_state(view)
        _set_panel_state(view, focus=st["focus"] + [target])
        render_view(view)
        return
    if kind == "pj":
        st = _panel_state(view)
        focus = [f for f in st["focus"] if A.norm(f) != A.norm(target)]
        if len(focus) != len(st["focus"]):
            _set_panel_state(view, focus=focus)  # focused → back to the shared budget
            render_view(view)
            return
        collapsed = [c for c in st["collapsed"] if A.norm(c) != A.norm(target)]
        if len(collapsed) == len(st["collapsed"]):
            collapsed.append(target)
        _set_panel_state(view, collapsed=collapsed)
        render_view(view)
        return
    open_target(view.window(), target)


def open_target(window, path):
    if not os.path.exists(path):
        sublime.status_message("Recent Activity: file no longer exists")
        return
    ext = os.path.splitext(path)[1].lower()
    if ext in A.OS_OPEN_EXTS:
        try:
            os.startfile(path)  # noqa: S606 - explicit user action on a listed file
        except OSError as exc:
            sublime.status_message(f"Recent Activity: cannot open ({exc})")
        return
    group = _main_group(window)
    view = window.open_file(path, group=group)
    window.focus_view(view)


def project_root_at(view, point=None):
    """The project root of the row under ``point``: a project header's own
    root, or the root a file row belongs to."""
    if point is None:
        sel = view.sel()
        point = sel[0].b if sel else 0
    t = target_at(view, point)
    if t is None:
        return None
    kind, target = t
    if kind == "pj":
        return target
    with _lock:
        ch = _model.changes.get(A.norm(target)) if _model is not None else None
    return ch.root if ch is not None else None


def hide_project_at(view, point=None):
    root = project_root_at(view, point)
    if not root:
        sublime.status_message("Recent Activity: no project under the caret")
        return
    save_manual_hidden(load_manual_hidden() + [root])
    sublime.status_message(f"Recent Activity: hid {os.path.basename(root)} — "
                           "restore with Show Hidden Projects")
    _render_all()


def unhide_all():
    n = len(load_manual_hidden())
    save_manual_hidden([])
    sublime.status_message(f"Recent Activity: restored {n} hidden project(s)"
                           + (" (the hide_projects setting still applies)"
                              if conf().get("hide_projects") else ""))
    _render_all()


def _main_group(window):
    panel = find_panel(window)
    if panel is None:
        return window.active_group()
    pg = window.get_view_index(panel)[0]
    if window.num_groups() > pg + 1:
        return pg + 1
    return max(0, pg - 1) if pg > 0 else window.active_group()


def mark_seen(view):
    _set_panel_state(view, last_seen=time.time())


def toggle_code(view):
    st = _panel_state(view)
    _set_panel_state(view, show_code=not st["show_code"])
    render_view(view)


def set_window_hours(view, hours):
    _set_panel_state(view, window_hours=float(hours))
    render_view(view)


def refresh(view):
    with _lock:
        if _model is not None:
            _model.set_sessions(A.load_sessions(sessions_dir()))
            _ingest_hook_log()
            _model.reattribute()
    render_view(view)


def flat_items(window_hours=None, show_code=None, window=None):
    """Newest-first list for the quick panel: ``[(label, detail, path)]``."""
    if _model is None:
        return []
    c = conf()
    hours = window_hours if window_hours is not None else float(c["window_hours"])
    code = c["show_code"] if show_code is None else show_code
    with _lock:
        tree = _model.tree(hours * 3600.0, show_code=code, within=_scope_dirs(window),
                           hidden=all_hidden())
    items = []
    for node in tree:
        for s in node["sessions"]:
            for ch in s["files"]:
                items.append((ch.ts, os.path.basename(ch.path),
                              "{}  ·  {}  ·  {}{}".format(A.fmt_time(ch.ts), node["label"],
                                                          s["label"],
                                                          f"  ⟨{ch.wt}⟩" if ch.wt else ""),
                              ch.path))
    items.sort(key=lambda i: i[0], reverse=True)
    return [(i[1], i[2], i[3]) for i in items]
