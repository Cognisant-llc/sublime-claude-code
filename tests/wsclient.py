"""Tiny raw-socket WebSocket client used by tests (and reusable for manual
smoke-testing against the plugin inside a real Sublime instance)."""

import json
import socket
import time

from claudeide.wsserver import OP_TEXT, decode_frames, encode_frame

RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="


class WSClient:
    def __init__(self, port, token, timeout=3.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(0.2)
        self.buf = bytearray()
        self.frames = []
        lines = [
            "GET / HTTP/1.1",
            "Host: 127.0.0.1:{}".format(port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: {}".format(RFC_KEY),
            "Sec-WebSocket-Version: 13",
            "x-claude-code-ide-authorization: {}".format(token),
        ]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        resp = b""
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in resp and time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError("handshake failed: {!r}".format(resp[:120]))

    def send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.sock.sendall(encode_frame(OP_TEXT, data, mask=True))

    def recv_json(self, timeout=3.0):
        """Next text frame parsed as JSON, or None on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            while self.frames:
                fin, opcode, payload = self.frames.pop(0)
                if opcode == OP_TEXT:
                    return json.loads(payload.decode("utf-8"))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return None
            self.buf.extend(chunk)
            self.frames.extend(decode_frames(self.buf))
        return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
