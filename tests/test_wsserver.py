import queue
import socket
import threading
import time

import pytest

from claudeide import wsserver
from claudeide.wsserver import (
    OP_CLOSE,
    OP_PING,
    OP_TEXT,
    WSServer,
    build_accept_response,
    compute_accept,
    decode_frames,
    encode_frame,
    parse_http_upgrade,
)

RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
RFC_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


# ---------- pure functions ----------


def test_compute_accept_rfc6455_vector():
    assert compute_accept(RFC_KEY) == RFC_ACCEPT


def test_parse_http_upgrade_lowercases_headers():
    raw = (
        b"GET /ws HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Upgrade: WebSocket\r\n"
        b"Sec-WebSocket-Key: " + RFC_KEY.encode() + b"\r\n"
        b"X-Claude-Code-IDE-Authorization: tok123\r\n\r\n"
    )
    req = parse_http_upgrade(raw)
    assert req["path"] == "/ws"
    assert req["headers"]["upgrade"] == "WebSocket"
    assert req["headers"]["sec-websocket-key"] == RFC_KEY
    assert req["headers"]["x-claude-code-ide-authorization"] == "tok123"


def test_build_accept_response_contains_accept_key():
    resp = build_accept_response(RFC_KEY)
    assert b"101" in resp
    assert RFC_ACCEPT.encode() in resp
    assert resp.endswith(b"\r\n\r\n")


@pytest.mark.parametrize("size", [0, 5, 125, 126, 200, 65535, 65536, 70000])
@pytest.mark.parametrize("mask", [True, False])
def test_frame_roundtrip_sizes(size, mask):
    payload = bytes(i % 251 for i in range(size))
    buf = bytearray(encode_frame(OP_TEXT, payload, mask=mask))
    frames = decode_frames(buf)
    assert frames == [(True, OP_TEXT, payload)]
    assert len(buf) == 0  # fully consumed


def test_decode_incremental_partial_then_complete():
    full = encode_frame(OP_TEXT, b"hello world", mask=True)
    buf = bytearray(full[:4])
    assert decode_frames(buf) == []  # incomplete: nothing yielded, buffer kept
    buf.extend(full[4:])
    frames = decode_frames(buf)
    assert frames == [(True, OP_TEXT, b"hello world")]


def test_decode_two_frames_in_one_buffer():
    buf = bytearray(encode_frame(OP_TEXT, b"a") + encode_frame(OP_PING, b"p"))
    frames = decode_frames(buf)
    assert frames == [(True, OP_TEXT, b"a"), (True, OP_PING, b"p")]


# ---------- live socket integration ----------


def _handshake(port, token, key=RFC_KEY):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    lines = [
        "GET / HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if token is not None:
        lines.append(f"x-claude-code-ide-authorization: {token}")
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    resp = b""
    deadline = time.time() + 3
    while b"\r\n\r\n" not in resp and time.time() < deadline:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        resp += chunk
    return s, resp


@pytest.fixture()
def server():
    inbox = queue.Queue()
    events = queue.Queue()
    srv = WSServer(
        auth_token="secret-token",
        on_message=inbox.put,
        on_connect=lambda: events.put("connect"),
        on_disconnect=lambda: events.put("disconnect"),
    )
    port = srv.start()
    yield srv, port, inbox, events
    srv.stop()


def test_rejects_wrong_token(server):
    srv, port, _, _ = server
    s, resp = _handshake(port, "wrong-token")
    assert b"401" in resp
    s.close()
    assert not srv.is_connected


def test_rejects_missing_token(server):
    srv, port, _, _ = server
    s, resp = _handshake(port, None)
    assert b"401" in resp
    s.close()


def test_accepts_correct_token_and_exchanges_messages(server):
    srv, port, inbox, events = server
    s, resp = _handshake(port, "secret-token")
    assert b"101" in resp
    assert RFC_ACCEPT.encode() in resp
    assert events.get(timeout=3) == "connect"
    assert srv.is_connected

    # client -> server (client frames must be masked)
    s.sendall(encode_frame(OP_TEXT, b'{"jsonrpc":"2.0","method":"hi"}', mask=True))
    assert inbox.get(timeout=3) == '{"jsonrpc":"2.0","method":"hi"}'

    # server -> client (server frames are not masked)
    srv.send_text('{"ok":1}')
    buf = bytearray()
    deadline = time.time() + 3
    frames = []
    while not frames and time.time() < deadline:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf.extend(chunk)
        frames = decode_frames(buf)
    assert frames == [(True, OP_TEXT, b'{"ok":1}')]

    # ping -> pong auto-reply
    s.sendall(encode_frame(OP_PING, b"p1", mask=True))
    buf2 = bytearray()
    pong = []
    deadline = time.time() + 3
    while not pong and time.time() < deadline:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf2.extend(chunk)
        pong = decode_frames(buf2)
    assert pong[0][1] == wsserver.OP_PONG
    assert pong[0][2] == b"p1"

    # close handshake -> disconnect callback
    s.sendall(encode_frame(OP_CLOSE, b"", mask=True))
    assert events.get(timeout=3) == "disconnect"
    s.close()


def test_port_is_in_claude_range(server):
    _, port, _, _ = server
    assert 10000 <= port <= 65535


def test_stop_is_idempotent_and_joins():
    srv = WSServer(auth_token="t", on_message=lambda m: None)
    srv.start()
    srv.stop()
    srv.stop()  # second stop must not raise
    # server socket must be released: starting a fresh server works
    srv2 = WSServer(auth_token="t", on_message=lambda m: None)
    srv2.start()
    srv2.stop()


def test_threads_are_daemon():
    srv = WSServer(auth_token="t", on_message=lambda m: None)
    srv.start()
    try:
        alive = [t for t in threading.enumerate() if t.name.startswith("claudeide-")]
        assert alive, "expected named claudeide- threads"
        assert all(t.daemon for t in alive)
    finally:
        srv.stop()
