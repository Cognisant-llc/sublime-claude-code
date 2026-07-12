"""Pending (deferred) request tracking for blocking tools like openDiff.

The reader thread registers a pending request and returns immediately;
the UI thread later calls :meth:`PendingRequests.resolve` with the outcome
("FILE_SAVED" / "DIFF_REJECTED") and the returned response dict is sent
back over the WebSocket.
"""

import threading
from typing import Any, Callable, Dict, List, Optional

from .mcp import tool_text_response


class PendingRequests:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = {}  # type: Dict[Any, Any]

    def add(self, req_id: Any, meta: Any = None) -> None:
        with self._lock:
            self._pending[req_id] = meta

    def get_meta(self, req_id: Any) -> Any:
        with self._lock:
            return self._pending.get(req_id)

    def find_by(self, predicate: Callable[[Any], bool]) -> Optional[Any]:
        with self._lock:
            for req_id, meta in self._pending.items():
                if predicate(meta):
                    return req_id
        return None

    def resolve(self, req_id: Any, text: str) -> Optional[Dict[str, Any]]:
        """Remove the pending entry and build its response. None if absent
        (already resolved) so double-resolution is harmless."""
        with self._lock:
            if req_id not in self._pending:
                return None
            del self._pending[req_id]
        return tool_text_response(req_id, text)

    def resolve_all(self, text: str) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(self._pending.keys())
            self._pending.clear()
        return [tool_text_response(req_id, text) for req_id in ids]
