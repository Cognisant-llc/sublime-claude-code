"""Minimal RFC 6455 WebSocket server for the Claude Code IDE protocol.

Scope is deliberately narrow — exactly what the protocol needs:
- localhost-only listener on a random port in [10000, 65535]
- upgrade handshake with ``x-claude-code-ide-authorization`` verification
- text frames + ping/pong + close, with client-side masking
- one client at a time (a new connection replaces the previous one)

Pure Python 3.8, standard library only, no asyncio: Sublime's plugin host
runs this in plain daemon threads.
"""

import base64
import hashlib
import hmac
import os
import random
import socket
import struct
import threading
from typing import Callable, Optional, Tuple

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
AUTH_HEADER = "x-claude-code-ide-authorization"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_MAX_HANDSHAKE = 64 * 1024
_MAX_FRAME = 64 * 1024 * 1024  # defensive cap against absurd length headers


# ---------- pure protocol functions (unit-tested directly) ----------


def compute_accept(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_http_upgrade(raw: bytes) -> dict:
    """Parse an HTTP upgrade request into {"path", "headers"} (header names
    lowercased, values stripped, original value case preserved)."""
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    path = parts[1] if len(parts) >= 2 else "/"
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return {"path": path, "headers": headers}


def build_accept_response(key: str, subprotocol: Optional[str] = None) -> bytes:
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {compute_accept(key)}",
    ]
    if subprotocol:
        lines.append(f"Sec-WebSocket-Protocol: {subprotocol}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def build_reject_response(status: int = 401, reason: str = "Unauthorized") -> bytes:
    body = reason.encode("ascii")
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    return head.encode("ascii") + body


def encode_frame(opcode: int, payload: bytes, mask: bool = False) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    ln = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if ln < 126:
        header.append(mask_bit | ln)
    elif ln <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", ln))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", ln))
    if mask:
        key = os.urandom(4)
        header.extend(key)
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + payload


def decode_frames(buf: bytearray):
    """Consume as many complete frames as available from ``buf`` (mutated
    in place). Returns a list of (fin, opcode, payload) tuples."""
    frames = []
    while True:
        if len(buf) < 2:
            return frames
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        idx = 2
        if ln == 126:
            if len(buf) < 4:
                return frames
            ln = struct.unpack("!H", bytes(buf[2:4]))[0]
            idx = 4
        elif ln == 127:
            if len(buf) < 10:
                return frames
            ln = struct.unpack("!Q", bytes(buf[2:10]))[0]
            idx = 10
        if ln > _MAX_FRAME:
            raise ValueError(f"frame too large: {ln}")
        need = idx + (4 if masked else 0) + ln
        if len(buf) < need:
            return frames
        if masked:
            key = bytes(buf[idx:idx + 4])
            idx += 4
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(buf[idx:idx + ln]))
        else:
            payload = bytes(buf[idx:idx + ln])
        del buf[:need]
        frames.append((fin, opcode, payload))


# ---------- threaded server ----------


class WSServer:
    """Single-client threaded WebSocket server bound to 127.0.0.1."""

    def __init__(
        self,
        auth_token: str,
        on_message: Callable[[str], None],
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        port_range: Tuple[int, int] = (10000, 65535),
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._auth_token = auth_token
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._port_range = port_range
        self._log = logger or (lambda msg: None)

        self._sock = None  # type: Optional[socket.socket]
        self._client = None  # type: Optional[socket.socket]
        self._client_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._running = False
        self._threads = []
        self.port = None  # type: Optional[int]

    # -- lifecycle --

    def start(self) -> int:
        lo, hi = self._port_range
        last_err = None
        for _ in range(64):
            port = random.randint(lo, hi)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                last_err = exc
                sock.close()
                continue
            sock.listen(1)
            sock.settimeout(0.5)
            self._sock = sock
            self.port = port
            break
        if self._sock is None:
            raise OSError(f"could not bind a port in {lo}-{hi}: {last_err}")

        self._running = True
        t = threading.Thread(target=self._accept_loop, name="claudeide-accept", daemon=True)
        t.start()
        self._threads.append(t)
        self._log(f"listening on 127.0.0.1:{self.port}")
        return self.port

    def stop(self) -> None:
        self._running = False
        self._drop_client(notify=False)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        for t in self._threads:
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=2)
        self._threads = []

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    # -- sending --

    def send_text(self, text: str) -> bool:
        with self._send_lock:
            client = self._client
            if client is None:
                return False
            try:
                client.sendall(encode_frame(OP_TEXT, text.encode("utf-8")))
                return True
            except OSError as exc:
                self._log(f"send failed: {exc}")
                self._drop_client()
                return False

    # -- internals --

    def _accept_loop(self) -> None:
        while self._running and self._sock is not None:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._log(f"connection from {addr}")
            t = threading.Thread(
                target=self._client_loop, args=(conn,), name="claudeide-client", daemon=True
            )
            t.start()
            self._threads.append(t)

    def _handshake(self, conn: socket.socket) -> bool:
        conn.settimeout(5)
        raw = b""
        try:
            while b"\r\n\r\n" not in raw and len(raw) < _MAX_HANDSHAKE:
                chunk = conn.recv(4096)
                if not chunk:
                    return False
                raw += chunk
        except OSError:
            return False

        req = parse_http_upgrade(raw)
        headers = req["headers"]
        key = headers.get("sec-websocket-key")
        supplied = headers.get(AUTH_HEADER, "")
        upgrade_ok = headers.get("upgrade", "").lower() == "websocket"
        auth_ok = bool(supplied) and hmac.compare_digest(supplied, self._auth_token)

        if not (upgrade_ok and key and auth_ok):
            self._log(f"handshake rejected (upgrade={upgrade_ok}, key={bool(key)}, auth={auth_ok})")
            try:
                conn.sendall(build_reject_response())
            except OSError:
                pass
            conn.close()
            return False

        subprotocol = None
        requested = headers.get("sec-websocket-protocol")
        if requested:
            subprotocol = requested.split(",")[0].strip()
        try:
            conn.sendall(build_accept_response(key, subprotocol))
        except OSError:
            conn.close()
            return False
        return True

    def _client_loop(self, conn: socket.socket) -> None:
        if not self._handshake(conn):
            return

        with self._client_lock:
            if self._client is not None:
                self._log("replacing previous client connection")
                self._drop_client()
            self._client = conn
        conn.settimeout(0.5)
        if self._on_connect:
            self._safe_callback(self._on_connect)

        buf = bytearray()
        fragments = bytearray()
        fragment_opcode = None
        while self._running and self._client is conn:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            try:
                frames = decode_frames(buf)
            except ValueError as exc:
                self._log(f"protocol error: {exc}")
                break
            closed = False
            for fin, opcode, payload in frames:
                if opcode == OP_PING:
                    with self._send_lock:
                        try:
                            conn.sendall(encode_frame(OP_PONG, payload))
                        except OSError:
                            closed = True
                            break
                elif opcode == OP_CLOSE:
                    with self._send_lock:
                        try:
                            conn.sendall(encode_frame(OP_CLOSE, payload[:2]))
                        except OSError:
                            pass
                    closed = True
                    break
                elif opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                    if opcode != OP_CONT:
                        fragment_opcode = opcode
                        fragments = bytearray()
                    fragments.extend(payload)
                    if fin and fragment_opcode == OP_TEXT:
                        text = fragments.decode("utf-8", errors="replace")
                        fragments = bytearray()
                        self._safe_callback(lambda t=text: self._on_message(t))
            if closed:
                break

        if self._client is conn:
            self._drop_client()
        else:
            try:
                conn.close()
            except OSError:
                pass

    def _drop_client(self, notify: bool = True) -> None:
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
            if notify and self._on_disconnect:
                self._safe_callback(self._on_disconnect)

    def _safe_callback(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - callbacks must not kill IO threads
            self._log(f"callback error: {exc}")
