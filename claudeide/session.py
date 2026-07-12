"""Pending (deferred) request tracking for blocking tools like openDiff.

Multiple Claude Code sessions may be connected at once, and JSON-RPC ids
are only unique per client — entries are therefore keyed by
``(client_id, request_id)``. The resolver returns the response dict for the
right client; senders route it with ``server.send_to(client_id, ...)``.
"""

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .mcp import tool_text_response


class PendingRequests:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = {}  # type: Dict[Tuple[Any, Any], Any]

    def add(self, client_id: Any, req_id: Any, meta: Any = None) -> None:
        with self._lock:
            self._pending[(client_id, req_id)] = meta

    def get_meta(self, client_id: Any, req_id: Any) -> Any:
        with self._lock:
            return self._pending.get((client_id, req_id))

    def find_by(self, predicate: Callable[[Any], bool]) -> Optional[Tuple[Any, Any]]:
        with self._lock:
            for key, meta in self._pending.items():
                if predicate(meta):
                    return key
        return None

    def resolve(self, client_id: Any, req_id: Any, payload: Any) -> Optional[Dict[str, Any]]:
        """Remove the entry and build its response. None if absent, so
        double-resolution is harmless. ``payload`` may be a string or a
        list of strings (multi-block content, e.g. ["FILE_SAVED", body])."""
        key = (client_id, req_id)
        with self._lock:
            if key not in self._pending:
                return None
            del self._pending[key]
        return tool_text_response(req_id, payload)

    @staticmethod
    def _outcome_with_meta(text: str, meta: Any) -> Any:
        """Blanket resolutions only know the outcome word; the reference
        client expects a second block naming the tab, which lives in meta."""
        if isinstance(meta, dict) and meta.get("tab_name"):
            return [text, meta["tab_name"]]
        return text

    def resolve_all_for(self, client_id: Any, text: str) -> List[Dict[str, Any]]:
        """Resolve everything belonging to one client (its disconnect)."""
        with self._lock:
            items = [(k, self._pending[k]) for k in self._pending if k[0] == client_id]
            for k, _meta in items:
                del self._pending[k]
        return [
            tool_text_response(req_id, self._outcome_with_meta(text, meta))
            for (_cid, req_id), meta in items
        ]

    def resolve_all(self, text: str) -> List[Tuple[Any, Dict[str, Any]]]:
        """Resolve everything; returns (client_id, response) pairs."""
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        return [
            (cid, tool_text_response(req_id, self._outcome_with_meta(text, meta)))
            for (cid, req_id), meta in items
        ]
