"""Session-badged tab grouping: slot assignment, titles and tab ordering (pure)."""

from claudeide import activity as A
from claudeide import tabgroup as T

# ---------- SlotMap ----------


def test_slots_are_stable_and_distinct():
    m = T.SlotMap()
    a = m.assign("a")
    b = m.assign("b")
    assert a == 1 and b == 2
    assert m.assign("a") == 1  # stable on repeat
    assert m.slot("a") == 1 and m.slot("zzz") == 0 and m.slot(None) == 0
    assert m.assign("") == 0 and m.assign(None) == 0


def test_slots_recycle_oldest_unprotected_when_full():
    m = T.SlotMap(slots=3)
    for sid in ("a", "b", "c"):
        m.assign(sid)
    # "a" is the oldest but protected (live / has tabs): "b" gives up its slot
    assert m.assign("d", keep={"a"}) == 2
    assert m.slot("b") == 0 and m.slot("a") == 1 and m.slot("c") == 3
    # nothing recyclable → no colour, and existing assignments untouched
    assert m.assign("e", keep={"a", "c", "d"}) == 0
    assert m.assigned() == {"a": 1, "c": 3, "d": 2}


def test_badge_glyphs():
    assert T.SLOTS == 8 and T.glyph(1) == "①" and T.glyph(8) == "⑧"
    assert T.glyph(0) == "" and T.glyph(9) == ""


def test_hues_and_scopes():
    assert len(T.HUES) == T.SLOTS and len(set(T.HUES)) == T.SLOTS
    assert T.region_scope(1) == "region.bluish" and T.region_scope(0) == ""
    assert T.scheme_file(2) == "claude-session-2.hidden-color-scheme"


def test_blend():
    assert T.blend("#000000", "#ffffff", 0.5) == "#808080"
    assert T.blend("#303841", "#6699cc", 0.0) == "#303841"
    assert T.blend("#303841", "#6699cc", 1.0) == "#6699cc"
    assert T.blend("#303841", "#6699cc", 5.0) == "#6699cc"  # clamped
    assert T.blend("#abc", "#000", 0.0) == "#aabbcc"  # short form
    assert T.blend("#30384180", "#6699cc", 0.0) == "#303841"  # alpha ignored


def test_tinted_scheme_copies_the_users_scheme():
    import json
    base = {"name": "Mariana", "variables": {"blue2": "hsla(210, 13%, 40%, 0.7)"},
            "globals": {"background": "var(blue3)", "line_highlight": "var(blue2)"},
            "rules": [{"scope": "comment", "foreground": "var(blue6)"}]}
    d = json.loads(T.scheme_json(base, "#303841", "#c695c6", 0.22))
    assert d["variables"] == base["variables"] and d["rules"] == base["rules"]
    assert d["globals"]["line_highlight"] == "var(blue2)"  # everything else kept
    assert d["globals"]["background"] == T.blend("#303841", "#c695c6", 0.22)
    assert base["globals"]["background"] == "var(blue3)"  # input untouched


# ---------- insert_index ----------


def test_insert_after_last_sibling():
    tabs = [(1, "s1"), (2, "s2"), (3, "s1"), (4, "")]
    # new tab 5 of s1 goes right after tab 3 (index 3 in the order without it)
    assert T.insert_index(tabs + [(5, "s1")], 5, "s1") == 3
    # a tab already adjacent needs no move
    assert T.insert_index([(1, "s1"), (3, "s1"), (2, "s2")], 3, "s1") is None
    # no sibling: leave it where it opened
    assert T.insert_index(tabs + [(6, "s9")], 6, "s9") is None
    # untagged tabs are never moved
    assert T.insert_index(tabs, 4, "") is None


def test_insert_moves_backwards_too():
    # tab 1 of s2 opened at the front; its sibling is tab 3
    tabs = [(1, "s2"), (2, "s1"), (3, "s2")]
    assert T.insert_index(tabs, 1, "s2") == 2  # after tab 3 in [2, 3]


# ---------- regroup / moves_for ----------


def test_regroup_clusters_by_first_appearance():
    tabs = [(1, "a"), (2, ""), (3, "b"), (4, "a"), (5, ""), (6, "b")]
    assert T.regroup(tabs) == [1, 4, 2, 5, 3, 6]


def test_moves_for_reproduces_regroup_order():
    tabs = [(1, "a"), (2, ""), (3, "b"), (4, "a"), (5, ""), (6, "b")]
    order = [vid for vid, _s in tabs]
    for vid, idx in T.moves_for(tabs):
        order.remove(vid)
        order.insert(idx, vid)
    assert order == T.regroup(tabs)
    assert T.moves_for([(1, "a"), (2, "a"), (3, "b")]) == []  # already grouped


# ---------- panel rows ----------


def test_session_marks():
    marks = T.session_marks({"s1": (2, 1), "s2": (0, 3), "s3": (1, 0)},
                            {"s1": 1, "s4": 4})
    assert marks == {"s1": "  ① ⧉2  ⊟1", "s2": "  ⊟3", "s3": "  ⧉1"}  # s4: slot but no tab
    assert T.session_marks({}, {}) == {}


def _tree(tmp_path):
    pj = str(tmp_path / "pj")
    model = A.ActivityModel()
    model.ingest_hook({"ts": 1000.0, "ev": "edit", "sid": "s1", "name": "one", "cwd": pj,
                       "path": str(tmp_path / "pj" / "a.md")})
    return model.tree(3600.0, now=1001.0), pj


def test_render_marks_session_rows_and_tab_counts(tmp_path):
    tree, _pj = _tree(tmp_path)
    text, targets = A.render(tree, [], 0.0, now=1001.0, width=40,
                             marks={"s1": "  ① ⧉2  ⊟1"})
    sess_rows = [row for row, t in targets.items() if t[0] == "sess"]
    assert sess_rows == [1] and targets[1] == ("sess", "s1")
    line = text.splitlines()[1]
    assert line.endswith("one  ① ⧉2  ⊟1")
    # without tab info the row is unchanged
    text2, _ = A.render(tree, [], 0.0, now=1001.0, width=40)
    assert text2.splitlines()[1].endswith("one")


def test_merge_schemes_applies_overrides_in_order():
    base = {"name": "Mariana", "variables": {"a": "#111", "b": "#222"},
            "globals": {"background": "#000", "foreground": "#fff"},
            "rules": [{"scope": "comment", "foreground": "var(a)"}]}
    override = {"variables": {"b": "#333"}, "rules": [{"scope": "markup.highlight", "background": "#444"}]}
    m = T.merge_schemes([base, override, "not a dict"])
    assert m["name"] == "Mariana" and m["variables"] == {"a": "#111", "b": "#333"}
    assert m["globals"] == {"background": "#000", "foreground": "#fff"}  # kept from the base
    assert [r["scope"] for r in m["rules"]] == ["comment", "markup.highlight"]
    assert T.merge_schemes([]) == {}

