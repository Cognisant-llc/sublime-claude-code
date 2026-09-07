"""Window layout arithmetic for the diff review UI. Pure Python, never
imports ``sublime``; the adapter applies the results with ``set_layout`` /
``set_view_index``.

A Sublime layout is ``{"cols": [x...], "rows": [y...], "cells": [[xmin, ymin,
xmax, ymax], ...]}`` where a cell's four numbers index into cols/rows and the
cell's position in the list is its group number.
"""

from typing import Any, Dict, List, Tuple

XMIN, YMIN, XMAX, YMAX = range(4)

Layout = Dict[str, Any]


def split_right(layout: Layout, group: int) -> Tuple[Layout, int]:
    """Split ``group`` into two side-by-side halves (Origami's "create pane
    right"). The original cell keeps its group number and its left half; the
    new right half is appended as the last group. Cells to the right shift
    over; cells spanning across the split widen. Returns ``(layout, new_group)``.
    """
    cols = list(layout["cols"])
    rows = list(layout["rows"])
    cells = [list(c) for c in layout["cells"]]
    old = cells.pop(group)
    for cell in cells:
        if cell[XMIN] >= old[XMAX]:
            cell[XMIN] += 1
        if cell[XMAX] >= old[XMAX]:
            cell[XMAX] += 1
    cols.insert(old[XMAX], (cols[old[XMIN]] + cols[old[XMAX]]) / 2.0)
    new = [old[XMAX], old[YMIN], old[XMAX] + 1, old[YMAX]]
    cells.insert(group, old)
    cells.append(new)
    return {"cols": cols, "rows": rows, "cells": cells}, len(cells) - 1


def restore_order(saved: List[Tuple[int, int, int]], alive: List[int],
                  num_groups: int) -> List[Tuple[int, int, int]]:
    """Which ``(view_id, group, index)`` moves to make after the original
    layout is back: saved positions of views that still exist, clamped to the
    groups that exist, in ascending (group, index) order so each index is
    valid when it is applied. ``alive`` = view ids present in the window now.
    """
    live = set(alive)
    out = []
    for vid, group, index in saved:
        if vid in live:
            out.append((vid, min(max(group, 0), num_groups - 1), index))
    out.sort(key=lambda t: (t[1], t[2]))
    return out
