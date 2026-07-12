from claudeide.diffcalc import changed_new_lines, pick_target_path


def test_changed_lines_replace_and_insert():
    old = "a\nb\nc\n"
    new = "a\nB\nc\nd\n"
    # line 1 replaced (b -> B), line 3 added (d)
    assert changed_new_lines(old, new) == [1, 3]


def test_changed_lines_identical():
    text = "x\ny\n"
    assert changed_new_lines(text, text) == []


def test_changed_lines_all_new_when_old_empty():
    assert changed_new_lines("", "a\nb\n") == [0, 1]


def test_changed_lines_deletion_only_marks_nothing_in_new():
    old = "a\nb\nc\n"
    new = "a\nc\n"
    assert changed_new_lines(old, new) == []


def test_pick_target_prefers_new_file_path():
    assert pick_target_path("C:\\old.py", "C:\\new.py") == "C:\\new.py"
    assert pick_target_path("C:\\old.py", None) == "C:\\old.py"
    assert pick_target_path("C:\\old.py", "") == "C:\\old.py"
