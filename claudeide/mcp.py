"""MCP (2025-03-26) server layer on top of JSON-RPC.

Implements ``initialize`` / ``tools/list`` / ``tools/call`` and wraps tool
results into MCP content arrays. Tool handlers are ``handler(arguments, ctx)``
and may return:

- ``str``   -> ``{"content": [{"type": "text", "text": <str>}]}``
- ``dict``/``list`` -> JSON-stringified into the text content
- ``DEFERRED``      -> no response now (resolved later, see session.py)

Raising :class:`ToolError` produces an ``isError: true`` content response
(protocol-level errors use JSON-RPC error objects instead).
"""

import json
from typing import Any, Callable, Dict, Optional

from . import jsonrpc
from .jsonrpc import DEFERRED, Dispatcher

PROTOCOL_VERSION = "2025-03-26"

ToolHandler = Callable[[Dict[str, Any], Dict[str, Any]], Any]


class ToolError(Exception):
    """Tool-level failure reported to Claude as isError content."""


def wrap_content(result: Any) -> Dict[str, Any]:
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


def tool_text_response(msg_id: Any, text: str) -> Dict[str, Any]:
    return jsonrpc.response(msg_id, wrap_content(text))


class MCPServer:
    def __init__(
        self,
        server_name: str = "Sublime Text",
        version: str = "0.1.0",
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._server_name = server_name
        self._version = version
        self._log = logger or (lambda msg: None)
        self._tools = {}  # type: Dict[str, Dict[str, Any]]
        self._handlers = {}  # type: Dict[str, ToolHandler]

        self.dispatcher = Dispatcher()
        self.dispatcher.register("initialize", self._initialize)
        self.dispatcher.register("notifications/initialized", lambda p, c: None)
        self.dispatcher.register("tools/list", self._tools_list)
        self.dispatcher.register("tools/call", self._tools_call)

    def register_tool(
        self, name: str, description: str, input_schema: Dict[str, Any], handler: ToolHandler
    ) -> None:
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
        }
        self._handlers[name] = handler

    # -- transport entry point --

    def handle_text(self, text: str) -> Optional[str]:
        """Handle one incoming frame; returns outgoing frame text or None."""
        try:
            message = jsonrpc.parse(text)
        except ValueError:
            return json.dumps(
                jsonrpc.error_response(None, jsonrpc.PARSE_ERROR, "parse error"),
                ensure_ascii=False,
            )
        self._log("<- {}".format(message.get("method") or "response"))
        resp = self.dispatcher.dispatch(message)
        if resp is None:
            return None
        return json.dumps(resp, ensure_ascii=False)

    # -- MCP methods --

    def _initialize(self, params: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        self._log(f"initialize from client: {json.dumps(params, ensure_ascii=False)}")
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": self._server_name, "version": self._version},
        }

    def _tools_list(self, params: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        return {"tools": list(self._tools.values())}

    def _tools_call(self, params: Any, ctx: Dict[str, Any]) -> Any:
        params = params or {}
        name = params.get("name")
        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        arguments = params.get("arguments") or {}
        try:
            result = handler(arguments, ctx)
        except ToolError as exc:
            wrapped = wrap_content(str(exc))
            wrapped["isError"] = True
            return wrapped
        if result is DEFERRED:
            return DEFERRED
        return wrap_content(result)
