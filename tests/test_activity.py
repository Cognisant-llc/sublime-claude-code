"""Unit tests for claudeide.activity (pure model + rendering) and the
hook script's pure parts."""

import json
import os

from claudeide import activity as A
from claudeide.fswatch import parse_notify_buffer
from scripts.activity_hook import record_from_hook

NOW = 1_788_500_000.0


def _root(tmp_path, name):
    r = tmp_path / name
    r.mkdir()
    return str(r)


# ---------- classification ----------


def test_ext_class_docs_media_code():
    assert A.ext_class("a/report.md") == "doc"
    assert A.ext_class("a/deck.pptx") == "doc"
    assert A.ext_class("a/sheet.xlsx") == "doc"
    assert A.ext_class("a/spec.docx") == "doc"
    assert A.ext_class("a/shot.PNG") == "media"
    assert A.ext_class("a/clip.mp4") == "media"
    assert A.ext_class("a/main.py") == "code"
    assert A.ext_class("a/data.json") == "code"


def test_temp_names_are_ignored():
    assert A.is_temp_name("~$deck.pptx")
    assert A.is_temp_name("x.tmp")
    assert A.is_temp_name("video.mp4.part")
    assert not A.is_temp_name("deck.pptx")


def test_is_pruned():
    assert A.is_pruned(["node_modules", "x"], A.DEFAULT_PRUNE)
    assert A.is_pruned([".claude", "worktrees", "a"], A.DEFAULT_PRUNE)
    assert not A.is_pruned(["docs", "a"], A.DEFAULT_PRUNE)


def test_fallback_label_uses_project_dir():
    assert A.fallback_label("2f383bbb-1", r"C:\w\shop-web") == "shop-web-2f38"
    assert A.fallback_label("2f383bbb-1", "") == "2f383bbb"


# ---------- roots / worktrees ----------


def test_root_index_classify_and_nesting(tmp_path):
    pj = _root(tmp_path, "gg_ds")
    idx = A.RootIndex()
    idx.add_root(pj)
    f = os.path.join(pj, "docs", "a.md")
    assert idx.classify(f) == (pj, "", os.path.join("docs", "a.md"))
    assert idx.classify(str(tmp_path / "elsewhere.md")) is None
    # a session started in a subdirectory is its own (more specific) project
    sub = os.path.join(pj, "docs")
    os.mkdir(sub)
    idx.add_root(sub)
    assert idx.roots() == [pj, sub]
    assert idx.classify(f) == (sub, "", "a.md")
    assert idx.watch_dirs() == [pj]


def test_nested_roots_keep_the_most_specific(tmp_path):
    outer = _root(tmp_path, "works")
    inner = os.path.join(outer, "pj")
    os.mkdir(inner)
    idx = A.RootIndex()
    idx.add_root(inner)
    idx.add_root(outer)
    assert idx.roots() == [outer, inner]
    assert idx.classify(os.path.join(inner, "a.md"))[0] == inner
    assert idx.classify(os.path.join(outer, "b.md"))[0] == outer
    assert idx.watch_dirs() == [outer]  # the watcher covers the outer only


def test_home_and_drive_roots_are_not_watched():
    home = os.path.expanduser("~")
    assert not A.is_watchable_root(home)
    assert not A.is_watchable_root(os.path.dirname(home))
    assert not A.is_watchable_root(os.path.splitdrive(home)[0] + os.sep)
    assert A.is_watchable_root(os.path.join(home, "work", "pj"))


def test_sibling_worktree_folds_into_main(tmp_path):
    pj = _root(tmp_path, "mbti")
    os.mkdir(os.path.join(pj, ".git"))
    wt = str(tmp_path / ".wt-infra")
    os.mkdir(wt)
    with open(os.path.join(wt, ".git"), "w") as fh:
        fh.write("gitdir: {}\n".format(os.path.join(pj, ".git", "worktrees", "infra")))
    assert A.worktree_main_root(wt) == pj
    idx = A.RootIndex()
    idx.add_root(pj)
    f = os.path.join(wt, "src", "x.md")
    assert idx.classify(f) == (pj, ".wt-infra", os.path.join("src", "x.md"))
    assert wt in idx.watch_dirs()


def test_session_started_inside_worktree(tmp_path):
    pj = _root(tmp_path, "mbti")
    os.mkdir(os.path.join(pj, ".git"))
    wt = str(tmp_path / "wt-a")
    os.mkdir(wt)
    with open(os.path.join(wt, ".git"), "w") as fh:
        fh.write("gitdir: {}\n".format(os.path.join(pj, ".git", "worktrees", "a")))
    idx = A.RootIndex()
    idx.add_root(wt)
    assert idx.roots() == [pj]
    assert idx.classify(os.path.join(wt, "r.md"))[1] == "wt-a"


def test_inner_subagent_worktree(tmp_path):
    pj = _root(tmp_path, "pj")
    idx = A.RootIndex()
    idx.add_root(pj)
    f = os.path.join(pj, ".claude", "worktrees", "agent-1", "docs", "a.md")
    assert idx.classify(f) == (pj, "agent-1", os.path.join("docs", "a.md"))


# ---------- log parsing ----------


def test_read_log_tail_handles_partial_and_shrink(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_bytes(b'{"ts": 1, "ev": "x"}\n{"ts": 2, "ev": "y"}\n{"ts": 3, "ev":')
    recs, off = A.read_log_tail(str(p), 0)
    assert [r["ev"] for r in recs] == ["x", "y"]
    p.write_bytes(p.read_bytes() + b' "z"}\n')
    recs, off = A.read_log_tail(str(p), off)
    assert [r["ev"] for r in recs] == ["z"]
    p.write_bytes(b'{"ts": 9, "ev": "w"}\n')  # compacted / shrunk
    recs, off = A.read_log_tail(str(p), off)
    assert [r["ev"] for r in recs] == ["w"]


def test_compact_log(tmp_path):
    p = tmp_path / "log.jsonl"
    old, new = int(NOW - 10_000), int(NOW - 10)
    p.write_text(f'{{"ts": {old}, "ev": "old"}}\n{{"ts": {new}, "ev": "new"}}\n')
    assert A.compact_log(str(p), keep_seconds=3600, now=NOW) == 1
    assert "new" in p.read_text() and "old" not in p.read_text()


# ---------- model: ingest + attribution ----------


def _model(tmp_path):
    pj = _root(tmp_path, "gg_ds")
    m = A.ActivityModel()
    m.set_sessions({
        "s1": A.SessionInfo("s1", "gg-ds-8e", pj, "busy"),
        "s2": A.SessionInfo("s2", "gg-ds-b6", pj, "idle"),
    })
    return m, pj


def test_edit_record_is_attributed_directly(tmp_path):
    m, pj = _model(tmp_path)
    f = os.path.join(pj, "STATUS.md")
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "s1", "cwd": pj, "path": f, "tool": "Edit"})
    ch = m.changes[A.norm(f)]
    assert ch.sid == "s1" and ch.source == "edit"


def test_fs_event_joins_edit_record_within_window(tmp_path):
    m, pj = _model(tmp_path)
    f = os.path.join(pj, "docs", "a.md")
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "s2", "cwd": pj, "path": f})
    m.ingest_fs(f, NOW + 1.5)
    ch = m.changes[A.norm(f)]
    assert ch.sid == "s2" and ch.count == 2 and ch.ts == NOW + 1.5


def test_fs_event_attributed_by_bash_interval(tmp_path):
    m, pj = _model(tmp_path)
    m.ingest_hook({"ts": NOW, "ev": "bash_start", "sid": "s1", "cwd": pj})
    m.ingest_hook({"ts": NOW + 30, "ev": "bash_end", "sid": "s1", "cwd": pj})
    f = os.path.join(pj, "out", "report.pdf")
    m.ingest_fs(f, NOW + 12)
    assert m.changes[A.norm(f)].sid == "s1"
    g = os.path.join(pj, "out", "later.pdf")
    m.ingest_fs(g, NOW + 300)
    assert m.changes[A.norm(g)].sid is None


def test_ambiguous_bash_overlap_stays_unattributed(tmp_path):
    m, pj = _model(tmp_path)
    for sid in ("s1", "s2"):
        m.ingest_hook({"ts": NOW, "ev": "bash_start", "sid": sid, "cwd": pj})
    f = os.path.join(pj, "x.md")
    m.ingest_fs(f, NOW + 5)
    assert m.changes[A.norm(f)].sid is None


def test_late_hook_record_reattributes(tmp_path):
    m, pj = _model(tmp_path)
    f = os.path.join(pj, "x.md")
    m.ingest_fs(f, NOW)
    assert m.changes[A.norm(f)].sid is None
    m.ingest_hook({"ts": NOW + 0.5, "ev": "edit", "sid": "s1", "cwd": pj, "path": f})
    assert m.changes[A.norm(f)].sid == "s1"


def test_pruned_and_temp_and_outside_are_dropped(tmp_path):
    m, pj = _model(tmp_path)
    m.ingest_fs(os.path.join(pj, "node_modules", "a", "b.md"), NOW)
    m.ingest_fs(os.path.join(pj, "~$deck.pptx"), NOW)
    m.ingest_fs(str(tmp_path / "other.md"), NOW)
    assert m.changes == {}


def test_removed_drops_entry(tmp_path):
    m, pj = _model(tmp_path)
    f = os.path.join(pj, "x.md")
    m.ingest_fs(f, NOW)
    m.ingest_fs(f, NOW + 1, "removed")
    assert m.changes == {}


def test_hook_log_root_is_learned_without_live_session(tmp_path):
    pj = _root(tmp_path, "ended_pj")
    m = A.ActivityModel()
    f = os.path.join(pj, "r.md")
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "gone", "name": "old-name",
                   "cwd": pj, "path": f})
    assert m.changes[A.norm(f)].sid == "gone"
    assert m.session_label("gone") == "old-name"
    assert m.session_status("gone") == "ended"
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "anon-1234", "cwd": pj, "path": f})
    assert m.session_label("anon-1234") == "ended_pj-anon"


# ---------- tree + render ----------


def test_tree_groups_and_orders(tmp_path):
    m, pj = _model(tmp_path)
    other = _root(tmp_path, "mbti")
    m.set_sessions({**{s.sid: s for s in m.sessions.values()},
                    "s3": A.SessionInfo("s3", "mbti-cf", other, "busy")})
    def edit(ts, sid, root, name):
        m.ingest_hook({"ts": ts, "ev": "edit", "sid": sid, "cwd": root,
                       "path": os.path.join(root, name)})
    edit(NOW - 100, "s1", pj, "a.md")
    edit(NOW - 50, "s2", pj, "b.md")
    edit(NOW - 10, "s3", other, "c.pptx")
    edit(NOW - 5, "s3", other, "d.py")
    tree = m.tree(3600, show_code=False, now=NOW)
    assert [n["label"] for n in tree] == ["mbti", "gg_ds"]
    assert tree[0]["count"] == 1  # d.py hidden
    assert [s["label"] for s in tree[1]["sessions"]] == ["gg-ds-b6", "gg-ds-8e"]
    assert tree[1]["live"] == 2
    tree_all = m.tree(3600, show_code=True, now=NOW)
    assert tree_all[0]["count"] == 2
    scoped = m.tree(3600, now=NOW, within=[pj])
    assert [n["label"] for n in scoped] == ["gg_ds"]
    assert m.tree(3600, now=NOW, within=[os.path.join(pj, "nowhere")]) == []
    assert m.tree(20, now=NOW)[0]["count"] == 1 and len(m.tree(20, now=NOW)) == 1


def test_render_targets_and_markers(tmp_path):
    m, pj = _model(tmp_path)
    f = os.path.join(pj, "docs", "deep", "dir", "report.md")
    m.ingest_hook({"ts": NOW - 5, "ev": "edit", "sid": "s1", "cwd": pj, "path": f})
    m.ingest_hook({"ts": NOW - 4, "ev": "edit", "sid": "s1", "cwd": pj, "path": f})
    tree = m.tree(3600, now=NOW)
    text, targets = A.render(tree, collapsed=[], last_seen=NOW - 10, now=NOW, width=40)
    lines = text.splitlines()
    assert lines[0] == "▼ gg_ds  1 ●2"
    assert lines[1] == "  ● gg-ds-8e"
    assert lines[2].startswith("  * ") and lines[2].endswith("report.md  ×2")
    assert targets[0] == ("pj", pj) and targets[2] == ("file", f)
    text_c, targets_c = A.render(tree, collapsed=[pj], last_seen=NOW, now=NOW)
    assert text_c.splitlines()[0].startswith("▶ gg_ds")
    assert list(targets_c.values()) == [("pj", pj)]


def test_elide_rel_keeps_name():
    assert A.elide_rel("a/b/c/d/report.md", 40) == "a/b/c/d/report.md"
    out = A.elide_rel("docs/status_archive/very_long_dir/deeper/2026-09.md", 30)
    assert out == "…/deeper/2026-09.md"
    assert A.elide_rel("docs/status_archive/very_long_dir/deeper/2026-09.md", 34) == \
        "…/very_long_dir/deeper/2026-09.md"
    assert A.elide_rel("skills/ui-design/SKILL.md", 20) == "…/ui-design/SKILL.md"
    assert A.elide_rel("x" * 50 + ".md", 20).endswith("…")
    assert A.dwidth("日本語.md") == 9 and A.dwidth("▼ a") == 4
    out = A.elide_rel("資料/送付ドラフト_臨時定例_進め方説明_20260904.md", 24)
    assert out.endswith("…") and A.dwidth(out) <= 24


def test_summary_line(tmp_path):
    m, pj = _model(tmp_path)
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "s1", "cwd": pj,
                   "path": os.path.join(pj, "a.md")})
    assert A.summary(m.tree(3600, now=NOW), 2) == "Δ1 · 1PJ · 2 live"


# ---------- hook script ----------


def test_hook_record_edit():
    rec = record_from_hook({"hook_event_name": "PostToolUse", "tool_name": "Write",
                            "session_id": "s", "cwd": "C:\\pj",
                            "tool_input": {"file_path": "C:\\pj\\out\\a.md", "content": "x"}},
                           now=NOW, name="pj-1")
    assert rec == {"ts": NOW, "sid": "s", "cwd": "C:\\pj", "name": "pj-1", "ev": "edit",
                   "path": os.path.normpath("C:\\pj\\out\\a.md"), "tool": "Write"}


def test_hook_record_bash_and_irrelevant():
    base = {"session_id": "s", "cwd": "C:\\pj", "tool_name": "Bash", "agent_type": "Explore"}
    start = record_from_hook({**base, "hook_event_name": "PreToolUse"}, now=NOW)
    assert start["ev"] == "bash_start"
    end = record_from_hook({**base, "hook_event_name": "PostToolUse"}, now=NOW)
    assert end["ev"] == "bash_end" and end["agent"] == "Explore"
    assert record_from_hook({**base, "tool_name": "Read", "hook_event_name": "PostToolUse"}) is None
    assert record_from_hook({**base, "tool_name": "Edit", "hook_event_name": "PreToolUse"}) is None
    assert json.dumps(end)  # serialisable


# ---------- fswatch buffer parsing ----------


def test_parse_notify_buffer_chain():
    def entry(next_off, action, name):
        b = name.encode("utf-16-le")
        return (next_off.to_bytes(4, "little") + action.to_bytes(4, "little")
                + len(b).to_bytes(4, "little") + b)
    first = entry(0, 3, "docs\\a.md")
    first = (len(first) + (4 - len(first) % 4) % 4).to_bytes(4, "little") + first[4:]
    first += b"\0" * ((4 - len(first) % 4) % 4)
    buf = first + entry(0, 1, "new.pptx")
    assert parse_notify_buffer(buf) == [(3, "docs\\a.md"), (1, "new.pptx")]


# ---------- session anchoring + hidden ----------


def test_session_anchored_to_first_seen_cwd(tmp_path):
    pj = _root(tmp_path, "gg_ds")
    sub = os.path.join(pj, "repos", "riku")
    os.makedirs(sub)
    m = A.ActivityModel()
    # first record = the open dir; a later record drifts into a subdirectory
    m.ingest_hook({"ts": NOW, "ev": "bash_start", "sid": "s1", "cwd": pj})
    m.ingest_hook({"ts": NOW + 1, "ev": "edit", "sid": "s1", "cwd": sub,
                   "path": os.path.join(sub, "a.md")})
    m.reattribute()  # the panel does this every cycle: rebuilds roots from anchors
    # only the open dir becomes a project; the subdir is not its own group
    assert m.roots.roots() == [pj]
    assert m.session_root("s1") == pj
    tree = m.tree(3600, now=NOW + 2)
    assert [n["label"] for n in tree] == ["gg_ds"]


def test_live_session_cwd_is_authoritative_over_drift(tmp_path):
    pj = _root(tmp_path, "mbti")
    sub = os.path.join(pj, "services", "llm")
    os.makedirs(sub)
    m = A.ActivityModel()
    m.set_sessions({"s1": A.SessionInfo("s1", "mbti-cf", pj, "busy")})
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "s1", "cwd": sub,
                   "path": os.path.join(sub, "x.md")})
    assert m.roots.roots() == [pj]
    assert m.session_root("s1") == pj


def test_is_hidden_by_basename_and_exact_path(tmp_path):
    pj = _root(tmp_path, "demo")
    assert A.is_hidden(pj, ["demo"])
    assert A.is_hidden(pj, [pj])
    # a nested project is NOT hidden by hiding its container (no prefix match)
    assert not A.is_hidden(os.path.join(pj, "sub"), [pj])
    assert not A.is_hidden(pj, ["other"])
    assert not A.is_hidden(pj, ["", None])


def test_tree_hidden_filters_projects(tmp_path):
    m, pj = _model(tmp_path)
    other = _root(tmp_path, "demo")
    m.set_sessions({**{s.sid: s for s in m.sessions.values()},
                    "s3": A.SessionInfo("s3", "demo-x", other, "busy")})
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "s1", "cwd": pj,
                   "path": os.path.join(pj, "a.md")})
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "s3", "cwd": other,
                   "path": os.path.join(other, "b.md")})
    assert {n["label"] for n in m.tree(3600, now=NOW)} == {"gg_ds", "demo"}
    assert [n["label"] for n in m.tree(3600, now=NOW, hidden=["demo"])] == ["gg_ds"]


# ---------- fs-log loggability + compaction bounds ----------


def test_is_fs_loggable_excludes_code_temp_and_system():
    assert A.is_fs_loggable(r"C:\w\pj\docs\a.md")
    assert A.is_fs_loggable(r"C:\w\pj\out\report.pdf")
    assert not A.is_fs_loggable(r"C:\w\pj\main.py")           # code
    assert not A.is_fs_loggable(r"C:\w\pj\~$deck.pptx")       # temp
    assert not A.is_fs_loggable(r"C:\Users\a\AppData\Local\Temp\x.md")  # system
    assert not A.is_fs_loggable(r"C:\Users\a\.claude\history.jsonl")    # .claude churn
    assert not A.is_fs_loggable(r"C:\w\pj\node_modules\p\readme.md")    # pruned


def test_ingest_fs_returns_accepted(tmp_path):
    m, pj = _model(tmp_path)
    assert m.ingest_fs(os.path.join(pj, "a.md"), NOW) is True
    assert m.ingest_fs(os.path.join(pj, "~$a.md"), NOW) is False       # temp dropped
    assert m.ingest_fs(str(tmp_path / "outside.md"), NOW) is False     # no root
    f = os.path.join(pj, "a.md")
    assert m.ingest_fs(f, NOW + 1, "removed") is True
    assert m.ingest_fs(f, NOW + 2, "removed") is False                 # already gone


def _write_log(p, records):
    p.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_compact_log_age_and_count(tmp_path):
    p = tmp_path / "log.jsonl"
    recs = [{"ts": NOW - i, "ev": "e", "path": f"C:/w/pj/f{i}.md"} for i in range(10)]
    recs.append({"ts": NOW - 100000, "ev": "old", "path": "x"})  # too old
    _write_log(p, recs)
    assert A.compact_log(str(p), keep_seconds=3600, now=NOW) == 10       # old dropped
    assert A.compact_log(str(p), keep_seconds=3600, now=NOW, max_records=4) == 4
    assert len(p.read_text().splitlines()) == 4


def test_compact_log_keep_pred(tmp_path):
    p = tmp_path / "log.jsonl"
    _write_log(p, [{"ts": NOW, "ev": "fs", "path": "C:/w/pj/a.md"},
                   {"ts": NOW, "ev": "fs", "path": "C:/w/pj/main.py"}])
    kept = A.compact_log(str(p), keep_seconds=3600, now=NOW,
                         keep_pred=lambda r: A.is_fs_loggable(r.get("path", "")))
    assert kept == 1 and "a.md" in p.read_text() and "main.py" not in p.read_text()


def test_trim_bounds_session_maps(tmp_path):
    m, pj = _model(tmp_path)
    # an ended session with a change that will age out
    m.ingest_hook({"ts": NOW - 100000, "ev": "edit", "sid": "old", "name": "old-s",
                   "cwd": pj, "path": os.path.join(pj, "gone.md")})
    assert "old" in m._cwds
    m.set_sessions({"s1": A.SessionInfo("s1", "gg-ds-8e", pj, "busy")})  # old no longer live
    m.trim(3600, now=NOW)
    assert "old" not in m._cwds and "old" not in m._names
    assert "s1" in m._cwds  # live session kept


# ---------- attribution cost + burst gate (0.3.5) ----------


def test_attribute_picks_nearest_edit_for_the_same_path(tmp_path):
    m, pj = _model(tmp_path)
    f = os.path.join(pj, "a.md")
    other = os.path.join(pj, "b.md")
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "s1", "cwd": pj, "path": other})
    m.ingest_hook({"ts": NOW - 4, "ev": "edit", "sid": "s2", "cwd": pj, "path": f})
    m.ingest_hook({"ts": NOW + 3, "ev": "edit", "sid": "s1", "cwd": pj, "path": f})
    m.ingest_fs(f, NOW + 4)
    assert m.changes[A.norm(f)].sid == "s1"  # 1 s away beats 8 s away; b.md never counts


def test_bash_attribution_walks_back_only_reachable_spans(tmp_path):
    m, pj = _model(tmp_path)
    # an old, closed span far in the past must not match; a later one does
    m.ingest_hook({"ts": NOW - 5000, "ev": "bash_start", "sid": "s1", "cwd": pj})
    m.ingest_hook({"ts": NOW - 4990, "ev": "bash_end", "sid": "s1", "cwd": pj})
    m.ingest_hook({"ts": NOW, "ev": "bash_start", "sid": "s1", "cwd": pj})
    m.ingest_hook({"ts": NOW + 10, "ev": "bash_end", "sid": "s1", "cwd": pj})
    f = os.path.join(pj, "out.pdf")
    m.ingest_fs(f, NOW + 5)
    assert m.changes[A.norm(f)].sid == "s1"
    g = os.path.join(pj, "old.pdf")
    m.ingest_fs(g, NOW - 3000)
    assert m.changes[A.norm(g)].sid is None


def test_reattribute_retries_recent_unattributed_only(tmp_path):
    m, pj = _model(tmp_path)
    old = os.path.join(pj, "old.md")
    new = os.path.join(pj, "new.md")
    m.ingest_fs(old, NOW - 2 * A.ActivityModel.RETRY_WINDOW)
    m.ingest_fs(new, NOW - 30)
    assert m.changes[A.norm(old)].sid is None and m.changes[A.norm(new)].sid is None
    # a bash span that would cover both arrives late (the hook log lagged)
    m.ingest_hook({"ts": NOW - 2 * A.ActivityModel.RETRY_WINDOW - 5, "ev": "bash_start",
                   "sid": "s1", "cwd": pj})
    m.ingest_hook({"ts": NOW, "ev": "bash_end", "sid": "s1", "cwd": pj})
    m.reattribute(now=NOW)
    assert m.changes[A.norm(new)].sid == "s1"
    assert m.changes[A.norm(old)].sid is None  # outside the retry window: left alone


def test_reattribute_reresolves_roots_only_when_anchors_move(tmp_path):
    pj = _root(tmp_path, "pj")
    sub = os.path.join(pj, "deep")
    os.makedirs(sub)
    m = A.ActivityModel()
    f = os.path.join(sub, "a.md")
    m.ingest_hook({"ts": NOW, "ev": "edit", "sid": "s1", "cwd": sub, "path": f})
    m.reattribute(now=NOW)
    assert m.changes[A.norm(f)].root == sub
    assert m._anchors_changed() is False  # settled: the next cycle skips the resolve pass
    m.ingest_hook({"ts": NOW + 1, "ev": "bash_start", "sid": "s1", "cwd": pj})  # shallower
    m.reattribute(now=NOW + 1)
    ch = m.changes[A.norm(f)]
    assert ch.root == pj and ch.rel == os.path.join("deep", "a.md")


def test_burst_gate_drops_storm_batches_until_quiet():
    g = A.BurstGate(threshold=100, quiet=5.0)
    assert g.batch("pj|", 39, NOW) is True  # the busiest genuine output seen
    assert g.batch("pj|", 830, NOW + 1) is False  # a worktree rebase
    assert g.batch("pj|", 3, NOW + 3) is False  # the storm's tail is still dropped
    assert g.batch("other|", 3, NOW + 3) is True  # other projects are unaffected
    assert g.batch("pj|", 2, NOW + 3 + 5.1) is True  # quiet again
    assert g.total_dropped() == 833 and g.dropped == {"pj|": 833}


def test_storm_buckets_and_burst_key(tmp_path):
    pj = _root(tmp_path, "pj")
    wt = os.path.join(str(tmp_path), ".wt-feature")
    os.makedirs(wt)
    roots = A.RootIndex()
    roots.add_root(pj)
    roots._register_wt(wt, pj)
    assert A.burst_key(os.path.join(pj, "a.md"), roots) == A.norm(pj) + "|"
    assert A.burst_key(os.path.join(wt, "a.md"), roots) == A.norm(pj) + "|.wt-feature"
    assert A.burst_key(r"C:\x\y\z\w\v.md", roots) == A.norm(r"C:\x\y\z")
    recs = [{"ts": NOW + i * 0.01, "path": os.path.join(wt, f"{i}.md")} for i in range(150)]
    recs += [{"ts": NOW + 0.5, "path": os.path.join(pj, "real.md")}]
    recs += [{"ts": NOW + 20, "path": os.path.join(wt, "later.md")}]
    storms = A.storm_buckets(recs, lambda p: A.burst_key(p, roots), window=10, threshold=100)
    assert storms == {(A.norm(pj) + "|.wt-feature", int(NOW // 10))}
    key = lambda r: (A.burst_key(r["path"], roots), int(r["ts"] // 10))  # noqa: E731
    assert key(recs[-1]) not in storms and key(recs[-2]) not in storms


# ---------- fit to view (0.3.5) ----------


def _big_tree(tmp_path, n_pj=3, n_sessions=2, n_files=6):
    """n_pj projects × n_sessions sessions × n_files files, newest project first."""
    m = A.ActivityModel()
    live = {}
    for p in range(n_pj):
        pj = _root(tmp_path, f"pj{p}")
        for s in range(n_sessions):
            sid = f"p{p}s{s}"
            live[sid] = A.SessionInfo(sid, sid, pj, "idle")
    m.set_sessions(live)
    for p in range(n_pj):
        pj = os.path.join(str(tmp_path), f"pj{p}")
        for s in range(n_sessions):
            for f in range(n_files):
                ts = NOW - p * 1000 - s * 100 - f  # newest project/session/file first
                m.ingest_hook({"ts": ts, "ev": "edit", "sid": f"p{p}s{s}", "cwd": pj,
                               "path": os.path.join(pj, f"s{s}f{f}.md")})
    return m, m.tree(3600, now=NOW + 1)


def test_plan_lines_unlimited_gives_every_session_the_cap(tmp_path):
    m, tree = _big_tree(tmp_path)
    quota, auto = A.plan_lines(tree, [], [], max_files=4, max_lines=None)
    assert auto == set() and set(quota.values()) == {4}


def test_plan_lines_round_robin_shows_every_session_first(tmp_path):
    m, tree = _big_tree(tmp_path)  # 3 pj × 2 sessions × 6 files
    # rows: header 2 + 3×(2 + 2×3) = 26 fixed; +4 rows = a second file for 4 sessions
    quota, auto = A.plan_lines(tree, [], [], max_files=20, max_lines=30)
    assert auto == set()
    per = [quota[(A.norm(n["root"]), s["sid"])] for n in tree for s in n["sessions"]]
    assert per == [2, 2, 2, 2, 1, 1]  # newest sessions got their second file first
    text, targets = A.render(tree, [], NOW, now=NOW, width=40, max_lines=30, header="h")
    assert len(text.rstrip("\n").split("\n")) <= 30
    assert text.count("… +") == 6  # every session still says how much is hidden


def test_plan_lines_folds_oldest_projects_when_rows_do_not_fit(tmp_path):
    m, tree = _big_tree(tmp_path)  # fixed rows = 24 without header
    quota, auto = A.plan_lines(tree, [], [], max_files=20, max_lines=18, header=False)
    assert auto == {A.norm(tree[-1]["root"])}  # only the oldest folded: 16 + 1 = 17 ≤ 18
    text, targets = A.render(tree, [], NOW, now=NOW, width=40, max_lines=18, header="")
    assert "▷ pj2" in text and "▼ pj1" in text and "▼ pj0" in text
    assert len(text.rstrip("\n").split("\n")) <= 18
    kinds = {t[1]: t[0] for t in targets.values() if t[0].startswith("pj")}
    assert kinds[tree[-1]["root"]] == "pj-auto" and kinds[tree[0]["root"]] == "pj"


def test_plan_lines_focus_is_filled_first_and_exempt_from_folding(tmp_path):
    m, tree = _big_tree(tmp_path)
    oldest = tree[-1]["root"]
    quota, auto = A.plan_lines(tree, [], [oldest], max_files=20, max_lines=18, header=False)
    assert A.norm(oldest) not in auto and auto == {A.norm(tree[-2]["root"])}
    quota, auto = A.plan_lines(tree, [], [oldest], max_files=3, max_lines=30, header=False)
    focused = [quota[(A.norm(oldest), s["sid"])] for s in tree[-1]["sessions"]]
    assert focused == [3, 3]  # the cap, before anyone else gets a second file
    text, _ = A.render(tree, [], NOW, now=NOW, width=40, max_lines=30, focus=[oldest])
    assert "◆ pj2" in text


def test_manual_collapse_costs_one_row_and_is_never_auto(tmp_path):
    m, tree = _big_tree(tmp_path)
    newest = tree[0]["root"]
    quota, auto = A.plan_lines(tree, [newest], [], max_files=20, max_lines=13, header=False)
    assert A.norm(newest) not in {k[0] for k in quota} and A.norm(newest) not in auto
    text, targets = A.render(tree, [newest], NOW, now=NOW, width=40, max_lines=13, header="")
    assert "▶ pj0" in text and len(text.rstrip("\n").split("\n")) <= 13
