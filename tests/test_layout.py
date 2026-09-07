"""Unit tests for claudeide.layout (pure window-layout arithmetic)."""

from claudeide import layout as L

ONE = {"cols": [0.0, 1.0], "rows": [0.0, 1.0], "cells": [[0, 0, 1, 1]]}
GRID = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 0.5, 1.0],
        "cells": [[0, 0, 1, 1], [1, 0, 2, 1], [0, 1, 1, 2], [1, 1, 2, 2]]}


def _valid(layout):
    ncol, nrow = len(layout["cols"]), len(layout["rows"])
    assert layout["cols"] == sorted(layout["cols"]) and layout["rows"] == sorted(layout["rows"])
    for xmin, ymin, xmax, ymax in layout["cells"]:
        assert 0 <= xmin < xmax < ncol and 0 <= ymin < ymax < nrow


def test_split_single_pane_becomes_two_columns():
    lay, new = L.split_right(ONE, 0)
    _valid(lay)
    assert new == 1
    assert lay["cols"] == [0.0, 0.5, 1.0]
    assert lay["cells"] == [[0, 0, 1, 1], [1, 0, 2, 1]]


def test_split_bottom_right_of_grid_keeps_other_groups():
    lay, new = L.split_right(GRID, 3)
    _valid(lay)
    assert new == 4
    assert lay["cols"] == [0.0, 0.5, 0.75, 1.0]
    # the three untouched groups keep their numbers; the top-right cell
    # widens across the inserted column; the split cell keeps its left half
    assert lay["cells"][:3] == [[0, 0, 1, 1], [1, 0, 3, 1], [0, 1, 1, 2]]
    assert lay["cells"][3] == [1, 1, 2, 2] and lay["cells"][4] == [2, 1, 3, 2]


def test_split_left_column_shifts_cells_to_its_right():
    lay, new = L.split_right(GRID, 0)
    _valid(lay)
    assert lay["cols"] == [0.0, 0.25, 0.5, 1.0]
    assert lay["cells"][1] == [2, 0, 3, 1] and lay["cells"][3] == [2, 1, 3, 2]
    assert lay["cells"][0] == [0, 0, 1, 1] and lay["cells"][new] == [1, 0, 2, 1]


def test_restore_order_drops_gone_views_clamps_groups_and_sorts():
    saved = [(1, 0, 0), (2, 1, 0), (3, 3, 1), (4, 3, 0), (5, 9, 0)]
    plan = L.restore_order(saved, alive=[1, 3, 4, 5], num_groups=4)
    assert plan == [(1, 0, 0), (4, 3, 0), (5, 3, 0), (3, 3, 1)]
