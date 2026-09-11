"""Session-grouped tabs (Sublime side).

Tabs opened by or for a Claude session carry the session id in their view
settings (``claude_session``); the Recent Activity panel shows the session's
badge and open-tab count after its row (a click focuses the newest tab).
Never ``View.set_name`` a file tab: it detaches the file. Tabs of one session
are kept adjacent; a
session's tabs can be focused, folded (closed and remembered on the window,
reopened as a set), closed, moved to the front/back of the pane, or their
paths copied — from the tab's context menu or the panel row.

Everything here runs on the main thread (event listeners and commands).
"""

import os
import time
from typing import Dict, List, Optional, Tuple

import sublime

from ..claudeide import tabgroup as T

SESSION_SETTING = "claude_session"
OPENED_SETTING = "claude_tab_opened"
FOLDED_SETTING = "claude_folded_tabs"   # window setting: {sid: [path, ...]}

_slots = T.SlotMap()
_live = set()  # type: set
_labels = {}  # type: Dict[str, str]


def conf():
    s = sublime.load_settings("Claude Code IDE.sublime-settings").get("session_tabs") or {}
    return {"badges": bool(s.get("badges", True)), "group": bool(s.get("group", True))}


# ---------- state fed by the activity panel ----------


def set_sessions(labels: Dict[str, str], live: List[str]) -> None:
    """Called by the panel on every poll with the current session list."""
    global _live
    _labels.update(labels)
    _live = set(live)


def label_for(sid: str) -> str:
    return _labels.get(sid) or (sid[:8] if sid else "—")


def _keep() -> set:
    keep = set(_live)
    for window in sublime.windows():
        for view in window.views():
            sid = view.settings().get(SESSION_SETTING)
            if sid:
                keep.add(sid)
    return keep


def slot_for(sid: Optional[str]) -> int:
    if not sid:
        return 0
    return _slots.assign(sid, keep=_keep())


def slots_of(sids) -> Dict[str, int]:
    """Current badge slots (read-only: a session with no tab takes none)."""
    return {sid: _slots.slot(sid) for sid in sids if _slots.slot(sid)}


# ---------- tagging & placement ----------


def sid_of(view) -> Optional[str]:
    if view is None or not view.is_valid():
        return None
    return view.settings().get(SESSION_SETTING) or None


def tag(view, sid: str) -> bool:
    """Mark ``view`` as belonging to ``sid``; returns True when it changed."""
    if view is None or not view.is_valid() or not sid:
        return False
    s = view.settings()
    if s.get(SESSION_SETTING) == sid:
        return False
    s.set(SESSION_SETTING, sid)
    s.set(OPENED_SETTING, time.time())
    if conf()["badges"]:
        slot_for(sid)  # take a badge slot while the session owns a tab
    return True


def _sid_of_sheet(sheet) -> str:
    view = sheet.view()
    return (view.settings().get(SESSION_SETTING) or "") if view is not None else ""


def _group_tabs(window, group: int) -> List[Tuple[int, str]]:
    """Tab order of a pane as ``(sheet id, sid)`` — sheets, not views, because
    image tabs have no view and still occupy a position."""
    return [(sh.id(), _sid_of_sheet(sh)) for sh in window.sheets_in_group(group)]


def place(window, view) -> None:
    """Move a freshly tagged tab next to its session's other tabs."""
    if not conf()["group"] or window is None or view is None or not view.is_valid():
        return
    sid = sid_of(view)
    if not sid:
        return
    sheet = view.sheet()
    if sheet is None:
        return
    group, _index = window.get_sheet_index(sheet)
    if group < 0:
        return
    target = T.insert_index(_group_tabs(window, group), sheet.id(), sid)
    if target is not None:
        window.set_sheet_index(sheet, group, target)


def tag_and_place(window, view, sid: Optional[str]) -> None:
    if sid and tag(view, sid):
        place(window, view)


def regroup_window(window) -> int:
    """Cluster every pane's tabs by session (explicit command). Returns the
    number of tabs moved."""
    moved = 0
    for group in range(window.num_groups()):
        tabs = _group_tabs(window, group)
        by_id = {sh.id(): sh for sh in window.sheets_in_group(group)}
        for sheet_id, index in T.moves_for(tabs):
            window.set_sheet_index(by_id[sheet_id], group, index)
            moved += 1
    return moved


# ---------- per-session queries ----------


def views_of(window, sid: str) -> list:
    return [v for v in window.views() if v.settings().get(SESSION_SETTING) == sid]


def sid_at(window, group: int, index: int) -> Optional[str]:
    """Session of the tab at a pane position (the tab context menu's args)."""
    sheets = window.sheets_in_group(group)
    if 0 <= index < len(sheets):
        return _sid_of_sheet(sheets[index]) or None
    return None


def tab_counts(window) -> Dict[str, Tuple[int, int]]:
    """``{sid: (open tabs, folded tabs)}`` for the panel's session rows."""
    out = {}  # type: Dict[str, Tuple[int, int]]
    for view in window.views():
        sid = view.settings().get(SESSION_SETTING)
        if sid:
            o, f = out.get(sid, (0, 0))
            out[sid] = (o + 1, f)
    for sid, paths in _folded(window).items():
        o, f = out.get(sid, (0, 0))
        out[sid] = (o, f + len(paths))
    return out


def panel_marks(window) -> Dict[str, str]:
    """Row suffixes for the panel (badge, open and folded tab counts)."""
    counts = tab_counts(window)
    slots = slots_of(list(counts) + list(_live)) if conf()["badges"] else {}
    return T.session_marks(counts, slots)


def focus_newest(window, sid: str) -> bool:
    views = views_of(window, sid)
    if not views:
        return False
    views.sort(key=lambda v: float(v.settings().get(OPENED_SETTING) or 0.0))
    window.focus_view(views[-1])
    return True


def close_session(window, sid: str) -> int:
    n = 0
    for view in views_of(window, sid):
        if view.is_dirty():
            continue  # never discard unsaved work silently
        view.close()
        n += 1
    return n


def paths_of(window, sid: str) -> List[str]:
    views = views_of(window, sid)
    views.sort(key=lambda v: window.get_view_index(v))
    return [v.file_name() for v in views if v.file_name()]


# ---------- fold / unfold (Chrome-style collapse, emulated) ----------


def _folded(window) -> Dict[str, List[str]]:
    return dict(window.settings().get(FOLDED_SETTING) or {})


def _set_folded(window, folded: Dict[str, List[str]]) -> None:
    window.settings().set(FOLDED_SETTING, folded)


def fold(window, sid: str) -> int:
    """Close the session's saved tabs and remember them on the window."""
    paths = paths_of(window, sid)
    if not paths:
        return 0
    folded = _folded(window)
    remembered = list(folded.get(sid, []))
    closed = 0
    for view in views_of(window, sid):
        path = view.file_name()
        if not path or view.is_dirty():
            continue
        if path not in remembered:
            remembered.append(path)
        view.close()
        closed += 1
    folded[sid] = remembered
    _set_folded(window, folded)
    return closed


def unfold(window, sid: str, group: Optional[int] = None) -> int:
    folded = _folded(window)
    paths = folded.pop(sid, [])
    _set_folded(window, folded)
    opened = 0
    for path in paths:
        if not os.path.exists(path):
            continue
        view = window.open_file(path, group=group if group is not None else -1)
        tag_and_place(window, view, sid)
        opened += 1
    return opened


def is_folded(window, sid: str) -> bool:
    return bool(_folded(window).get(sid))


# ---------- moving a whole session ----------


def move_session(window, sid: str, where: str) -> int:
    """``where``: "front" | "back" of the pane each tab is in."""
    sheets = [v.sheet() for v in views_of(window, sid)]
    sheets = [sh for sh in sheets if sh is not None]
    sheets.sort(key=lambda sh: window.get_sheet_index(sh))
    if where == "back":
        sheets.reverse()
    moved = 0
    for sheet in sheets:
        group, _i = window.get_sheet_index(sheet)
        if group < 0:
            continue
        n = len(window.sheets_in_group(group))
        window.set_sheet_index(sheet, group, 0 if where == "front" else n - 1)
        moved += 1
    return moved
