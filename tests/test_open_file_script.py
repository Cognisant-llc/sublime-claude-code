"""Unit tests for scripts/open_file.py (pure parts: argv mapping, response
parsing, notification-skipping). The wire itself is covered by test_wsserver
and the live smoke scripts."""

import os

import pytest

from scripts.open_file import (
    ToolCallError,
    extract_tool_result,
    request_from_argv,
    wait_response,
)


def _ok(msg_id, text, is_error=False):
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


# ---------- request_from_argv ----------


def test_request_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = request_from_argv(["report.md"])
    assert args == {
        "filePath": os.path.join(str(tmp_path), "report.md"),
        "preview": False,
        "makeFrontmost": True,
    }


def test_request_absolute_path_kept(tmp_path):
    target = str(tmp_path / "深い" / "日本語 レポート.md")
    args = request_from_argv([target])
    assert args["filePath"] == target


def test_preview_implies_no_focus():
    args = request_from_argv([r"C:\x\a.md", "--preview"])
    assert args["preview"] is True
    assert args["makeFrontmost"] is False


def test_no_focus_keeps_normal_tab():
    args = request_from_argv([r"C:\x\a.md", "--no-focus"])
    assert args["preview"] is False
    assert args["makeFrontmost"] is False


def test_selection_args_passed_through():
    args = request_from_argv([
        r"C:\x\a.md", "--start-text", "## Summary",
        "--end-text", "## Details", "--select-to-eol",
    ])
    assert args["startText"] == "## Summary"
    assert args["endText"] == "## Details"
    assert args["selectToEndOfLine"] is True


def test_selection_absent_by_default():
    args = request_from_argv([r"C:\x\a.md"])
    for key in ("startText", "endText", "selectToEndOfLine"):
        assert key not in args


def test_end_text_without_start_text_ignored():
    args = request_from_argv([r"C:\x\a.md", "--end-text", "X", "--select-to-eol"])
    for key in ("startText", "endText", "selectToEndOfLine"):
        assert key not in args


# ---------- extract_tool_result ----------


def test_extract_plain_text():
    assert extract_tool_result(_ok(2, "Opened file: C:\\x\\a.md")) == "Opened file: C:\\x\\a.md"


def test_extract_is_error_raises():
    with pytest.raises(ToolCallError, match="file not found"):
        extract_tool_result(_ok(2, "file not found: C:\\x\\a.md", is_error=True))


def test_extract_jsonrpc_error_raises():
    resp = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "unknown tool"}}
    with pytest.raises(ToolCallError, match="unknown tool"):
        extract_tool_result(resp)


def test_extract_none_raises():
    with pytest.raises(ToolCallError, match="no response"):
        extract_tool_result(None)


# ---------- wait_response ----------


class FakeClient:
    def __init__(self, frames):
        self.frames = list(frames)

    def recv_json(self, timeout=0):
        return self.frames.pop(0) if self.frames else None


def test_wait_response_skips_notifications():
    notify = {"jsonrpc": "2.0", "method": "selection_changed", "params": {}}
    client = FakeClient([notify, notify, _ok(7, "done")])
    resp = wait_response(client, 7, timeout=2.0)
    assert resp is not None and resp["id"] == 7


def test_wait_response_timeout_returns_none():
    client = FakeClient([{"jsonrpc": "2.0", "method": "selection_changed", "params": {}}])
    assert wait_response(client, 7, timeout=0.3) is None
