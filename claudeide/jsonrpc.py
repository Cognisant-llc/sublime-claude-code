"""Minimal JSON-RPC 2.0 message building and dispatch.

Handlers are registered per method and called as ``handler(params, ctx)``
where ``ctx`` is ``{"id": <request id or None>}``. A handler may return
``DEFERRED`` to signal that the response will be produced later by another
component (used for blocking tools like openDiff).
"""

import json
from typing import Any, Callable, Dict, Optional

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: Sentinel: handler will respond later via an out-of-band resolve.
DEFERRED = object()

Handler = Callable[[Any, Dict[str, Any]], Any]


def parse(text: str) -> Dict[str, Any]:
    return json.loads(text)


def request(msg_id: Any, method: str, params: Any = None) -> Dict[str, Any]:
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def notification(method: str, params: Any = None) -> Dict[str, Any]:
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def response(msg_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def error_response(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class Dispatcher:
    def __init__(self) -> None:
        self._handlers = {}  # type: Dict[str, Handler]

    def register(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    def dispatch(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Route one parsed message. Returns a response dict, or None when
        no response should be sent (notification, or DEFERRED handler)."""
        method = message.get("method")
        msg_id = message.get("id")
        is_request = "id" in message

        handler = self._handlers.get(method)
        if handler is None:
            if is_request:
                return error_response(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")
            return None

        try:
            result = handler(message.get("params"), {"id": msg_id})
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            if is_request:
                return error_response(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            return None

        if not is_request:
            return None
        if result is DEFERRED:
            return None
        return response(msg_id, result)
