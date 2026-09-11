"""Session-grouped tabs: pure logic (no Sublime imports).

Every session that owns open tabs gets one of ``SLOTS`` badge slots; the
Recent Activity panel shows the badge and the open/folded tab counts after the
session's row (``① ⧉3``). The tab bar itself cannot show a per-tab mark:
theme ``settings`` selectors only read global settings (verified: a view
setting never matches), and ``View.set_name`` on a file tab detaches the
file from the view (``file_name()`` becomes None — verified, never use it).

Tabs of one session are also kept adjacent: a newly opened tab is placed right
after the last tab of its session (``insert_index``); tabs the user dragged are
never moved again unless ``regroup`` is asked for explicitly.
"""

from typing import Dict, List, Optional, Sequence, Tuple

GLYPHS = "①②③④⑤⑥⑦⑧"
SLOTS = len(GLYPHS)


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
        g = glyph(slots.get(sid, 0))
        if g:
            parts.append(g + (f" ⧉{opened}" if opened else ""))
        elif opened:
            parts.append(f"⧉{opened}")
        if folded:
            parts.append(f"⊟{folded}")
        if parts:
            out[sid] = "  " + "  ".join(parts)
    return out
