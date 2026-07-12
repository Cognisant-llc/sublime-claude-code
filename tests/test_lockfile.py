import json
import re

from claudeide import lockfile


def test_generate_token_is_32_lowercase_hex():
    tok = lockfile.generate_token()
    assert re.fullmatch(r"[0-9a-f]{32}", tok)
    assert lockfile.generate_token() != tok  # random


def test_write_and_read_lock(tmp_path):
    path = lockfile.write_lock(
        port=12345,
        pid=999,
        workspace_folders=["C:\\proj\\a", "C:\\proj\\b"],
        auth_token="a" * 32,
        ide_name="Sublime Text",
        lock_dir=str(tmp_path),
    )
    assert path.endswith("12345.lock")
    data = json.loads(open(path, encoding="utf-8").read())
    assert data == {
        "pid": 999,
        "workspaceFolders": ["C:\\proj\\a", "C:\\proj\\b"],
        "ideName": "Sublime Text",
        "transport": "ws",
        "authToken": "a" * 32,
    }


def test_write_lock_overwrites_atomically(tmp_path):
    lockfile.write_lock(1, 1, ["x"], "a" * 32, lock_dir=str(tmp_path))
    lockfile.write_lock(1, 2, ["y"], "b" * 32, lock_dir=str(tmp_path))
    data = json.loads(open(str(tmp_path / "1.lock"), encoding="utf-8").read())
    assert data["pid"] == 2
    assert data["workspaceFolders"] == ["y"]
    assert not list(tmp_path.glob("*.tmp"))  # no temp litter


def test_remove_lock(tmp_path):
    lockfile.write_lock(7, 1, [], "c" * 32, lock_dir=str(tmp_path))
    assert (tmp_path / "7.lock").exists()
    lockfile.remove_lock(7, lock_dir=str(tmp_path))
    assert not (tmp_path / "7.lock").exists()
    lockfile.remove_lock(7, lock_dir=str(tmp_path))  # idempotent


def test_default_lock_dir_is_under_home():
    d = lockfile.default_lock_dir()
    assert d.replace("\\", "/").endswith(".claude/ide")
