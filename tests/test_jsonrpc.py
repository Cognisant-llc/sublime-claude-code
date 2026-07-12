import json

from claudeide import jsonrpc
from claudeide.jsonrpc import DEFERRED, Dispatcher


def test_response_shape():
    assert jsonrpc.response(1, {"ok": True}) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_error_response_shape():
    r = jsonrpc.error_response(2, jsonrpc.METHOD_NOT_FOUND, "nope")
    assert r == {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "nope"}}


def test_notification_shape():
    n = jsonrpc.notification("selection_changed", {"text": "x"})
    assert n == {"jsonrpc": "2.0", "method": "selection_changed", "params": {"text": "x"}}
    assert "id" not in n


def test_dispatch_routes_request_to_handler():
    d = Dispatcher()
    d.register("add", lambda params, ctx: params["a"] + params["b"])
    resp = d.dispatch({"jsonrpc": "2.0", "id": 7, "method": "add", "params": {"a": 1, "b": 2}})
    assert resp == {"jsonrpc": "2.0", "id": 7, "result": 3}


def test_dispatch_unknown_request_returns_method_not_found():
    d = Dispatcher()
    resp = d.dispatch({"jsonrpc": "2.0", "id": 8, "method": "nope"})
    assert resp["error"]["code"] == jsonrpc.METHOD_NOT_FOUND
    assert resp["id"] == 8


def test_dispatch_unknown_notification_is_ignored():
    d = Dispatcher()
    resp = d.dispatch({"jsonrpc": "2.0", "method": "nope"})
    assert resp is None


def test_dispatch_notification_returns_none_even_with_handler():
    seen = []
    d = Dispatcher()
    d.register("ping", lambda params, ctx: seen.append(params) or "ignored")
    resp = d.dispatch({"jsonrpc": "2.0", "method": "ping", "params": {"x": 1}})
    assert resp is None
    assert seen == [{"x": 1}]


def test_dispatch_deferred_returns_none_for_request():
    d = Dispatcher()
    d.register("slow", lambda params, ctx: DEFERRED)
    resp = d.dispatch({"jsonrpc": "2.0", "id": 9, "method": "slow"})
    assert resp is None


def test_dispatch_handler_exception_becomes_internal_error():
    d = Dispatcher()

    def boom(params, ctx):
        raise RuntimeError("kaboom")

    d.register("boom", boom)
    resp = d.dispatch({"jsonrpc": "2.0", "id": 10, "method": "boom"})
    assert resp["error"]["code"] == jsonrpc.INTERNAL_ERROR
    assert "kaboom" in resp["error"]["message"]


def test_ctx_carries_request_id():
    d = Dispatcher()
    d.register("who", lambda params, ctx: ctx["id"])
    resp = d.dispatch({"jsonrpc": "2.0", "id": "req-42", "method": "who"})
    assert resp["result"] == "req-42"


def test_parse_valid_and_invalid():
    msg = jsonrpc.parse('{"jsonrpc":"2.0","id":1,"method":"m"}')
    assert msg["method"] == "m"
    import pytest

    with pytest.raises(json.JSONDecodeError):
        jsonrpc.parse("{not json")
