"""Session-grouped tabs: pure logic (no Sublime imports).

Every session that owns open tabs gets one of ``SLOTS`` badge slots; the
Recent Activity panel shows the badge and the open/folded tab counts after the
session's row (``① ⧉3``) and underlines it in the slot's colour.

The tab itself is coloured through the one channel Sublime offers: a tab is
tinted with the background colour of *its own view's* colour scheme
(``tab_control`` ``tint_index``). So every slot gets a hidden colour scheme
that extends the user's scheme with a hued background, tagged views get that
scheme, and a theme rule shipped with the package shows the tint layer on
unselected tabs too. (Verified 2026-09-12. Dead ends, verified as well: theme
``settings`` selectors only read global settings, and ``View.set_name`` on a
file tab detaches the file — never use either.)

Tabs of one session are also kept adjacent: a newly opened tab is placed right
after the last tab of its session (``insert_index``); tabs the user dragged are
never moved again unless ``regroup`` is asked for explicitly.
"""

from typing import Dict, List, Optional, Sequence, Tuple

GLYPHS = "①②③④⑤⑥⑦⑧"
SLOTS = len(GLYPHS)

# slot -> colour-scheme palette entry (every scheme defines region.<name>, and
# sublime.ui_info() reports the resolved values), in an order that keeps
# neighbouring slots far apart on the hue wheel
HUES = ("bluish", "orangish", "greenish", "purplish", "redish", "cyanish", "yellowish", "pinkish")
DEFAULT_TINT = 0.14
SCHEME_PREFIX = "claude-session-"


def hue_name(slot: int) -> str:
    return HUES[slot - 1] if 1 <= slot <= SLOTS else ""


def region_scope(slot: int) -> str:
    """Scope that paints the slot's hue in a view (panel underline)."""
    return "region." + hue_name(slot) if hue_name(slot) else ""


def scheme_file(slot: int) -> str:
    return f"{SCHEME_PREFIX}{slot}.hidden-color-scheme"


def parse_hex(color: str) -> Tuple[int, int, int]:
    c = color.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) not in (6, 8):
        raise ValueError(color)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def blend(base: str, hue: str, t: float) -> str:
    """``base`` moved ``t`` (0..1) of the way towards ``hue``, as ``#rrggbb``."""
    t = max(0.0, min(1.0, float(t)))
    b, h = parse_hex(base), parse_hex(hue)
    return "#" + "".join(f"{round(b[i] * (1 - t) + h[i] * t):02x}" for i in range(3))


def merge_schemes(parts: Sequence[dict]) -> dict:
    """Same-named colour-scheme resources combined in load order, the way
    Sublime applies overrides: variables and globals update, rules append.
    (A partial override in Packages/User read alone has no foreground.)"""
    merged = {}  # type: dict
    for data in parts:
        if not isinstance(data, dict):
            continue
        for key in ("name", "author", "extends"):
            if key in data:
                merged[key] = data[key]
        merged.setdefault("variables", {}).update(data.get("variables") or {})
        merged.setdefault("globals", {}).update(data.get("globals") or {})
        merged.setdefault("rules", []).extend(data.get("rules") or [])
    return merged


def tinted_scheme(base: dict, background: str, hue: str, tint: float) -> dict:
    """Copy of the user's colour scheme (already decoded) with a hued
    background. Copying keeps variables, globals and rules intact — an
    ``extends`` scheme loses the base's variables (line highlight etc. fall
    back to defaults, verified)."""
    import copy
    out = copy.deepcopy(base)
    out["name"] = "Claude session tint"
    out.setdefault("globals", {})["background"] = blend(background, hue, tint)
    return out


def scheme_json(base: dict, background: str, hue: str, tint: float) -> str:
    import json
    return json.dumps(tinted_scheme(base, background, hue, tint), indent=1,
                      ensure_ascii=False) + chr(10)


def glyph(slot: int) -> str:
    return GLYPHS[slot - 1] if 1 <= slot <= SLOTS else ""


class SlotMap:
    """Stable ``session id -> badge slot`` assignment.

    A session keeps its slot for as long as it is assigned. When every slot is
    taken, the oldest assignment whose session is not in ``keep`` (live
    sessions, sessions that still own an open tab) is recycled; if nothing can
    be recycled the session gets slot 0 = no badge.
    """

    def __init__(self, slots: int = SLOTS):
        self.slots = slots
        self._map = {}  # type: Dict[str, int]
        self._order = []  # type: List[str]   assignment order, oldest first

    def slot(self, sid: Optional[str]) -> int:
        return self._map.get(sid or "", 0)

    def assign(self, sid: Optional[str], keep: Sequence[str] = ()) -> int:
        if not sid:
            return 0
        got = self._map.get(sid)
        if got:
            return got
        used = set(self._map.values())
        free = [s for s in range(1, self.slots + 1) if s not in used]
        if free:
            slot = free[0]
        else:
            protected = set(keep)
            victim = next((old for old in self._order if old not in protected), None)
            if victim is None:
                return 0
            slot = self._map.pop(victim)
            self._order.remove(victim)
        self._map[sid] = slot
        self._order.append(sid)
        return slot

    def assigned(self) -> Dict[str, int]:
        return dict(self._map)


def insert_index(tabs: Sequence[Tuple[int, str]], view_id: int, sid: str) -> Optional[int]:
    """Where a tab should sit so it is adjacent to its session's other tabs.

    ``tabs`` is the group's current order as ``(view_id, sid_or_empty)``; the
    tab being placed may or may not already be in it. Returns the target index
    (in the order with the tab removed, i.e. what ``set_view_index`` expects),
    or ``None`` when no move is needed (no sibling tab, or already adjacent).
    """
    if not sid:
        return None
    rest = [(vid, s) for vid, s in tabs if vid != view_id]
    cur = next((i for i, (vid, _s) in enumerate(tabs) if vid == view_id), None)
    last = None
    for i, (_vid, s) in enumerate(rest):
        if s == sid:
            last = i
    if last is None:
        return None
    target = last + 1
    if cur is not None and target == cur:
        return None
    return target


def regroup(tabs: Sequence[Tuple[int, str]]) -> List[int]:
    """Full order that clusters every session's tabs together, sessions in
    order of first appearance; untagged tabs form their own cluster where the
    first of them was. Returns view ids in the new order."""
    buckets = {}  # type: Dict[str, List[int]]
    order = []  # type: List[str]
    for vid, sid in tabs:
        key = sid or ""
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(vid)
    out = []  # type: List[int]
    for key in order:
        out.extend(buckets[key])
    return out


def moves_for(tabs: Sequence[Tuple[int, str]]) -> List[Tuple[int, int]]:
    """``(view_id, index)`` pairs that turn ``tabs`` into ``regroup(tabs)``
    when applied in order with ``set_view_index``."""
    wanted = regroup(tabs)
    current = [vid for vid, _s in tabs]
    moves = []  # type: List[Tuple[int, int]]
    for idx, vid in enumerate(wanted):
        if current[idx] == vid:
            continue
        current.remove(vid)
        current.insert(idx, vid)
        moves.append((vid, idx))
    return moves


def session_marks(counts: Dict[str, Tuple[int, int]], slots: Dict[str, int]) -> Dict[str, str]:
    """Suffix for a panel session row: ``"  ① ⧉2"`` (badge, open tabs) and
    ``"  ⊟3"`` (folded tabs). Sessions without tabs get the badge only when
    they hold a slot."""
    out = {}  # type: Dict[str, str]
    for sid in set(counts) | set(slots):
        opened, folded = counts.get(sid, (0, 0))
        parts = []
        g = glyph(slots.get(sid, 0)) if (opened or folded) else ""  # badge only with tabs
        if g:
            parts.append(g + (f" ⧉{opened}" if opened else ""))
        elif opened:
            parts.append(f"⧉{opened}")
        if folded:
            parts.append(f"⊟{folded}")
        if parts:
            out[sid] = "  " + "  ".join(parts)
    return out
