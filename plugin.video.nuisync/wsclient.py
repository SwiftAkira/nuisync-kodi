"""
wsclient.py — Minimal WebSocket client for NuiSync~

Pure Python, no external dependencies. Supports wss:// (TLS).
Implements RFC 6455 just enough for text frames + ping/pong.
"""

import socket
import ssl
import os
import struct
import base64
import hashlib
import threading

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

# Opcodes
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketClient(object):
    """Minimal WebSocket client for connecting to the NuiSync relay."""

    def __init__(self, url, timeout=10):
        self._url = url
        self._timeout = timeout
        self._sock = None
        self._lock = threading.Lock()
        self._connected = False

    @property
    def connected(self):
        return self._connected

    def connect(self):
        """Connect and perform the WebSocket handshake."""
        parsed = urlparse(self._url)
        use_ssl = parsed.scheme == "wss"
        host = parsed.hostname
        port = parsed.port or (443 if use_ssl else 80)
        path = parsed.path or "/"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)

        if use_ssl:
            ctx = ssl.create_default_context()
            ctx.set_alpn_protocols(["http/1.1"])
            sock = ctx.wrap_socket(sock, server_hostname=host)

        sock.connect((host, port))

        # WebSocket upgrade handshake
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, host, key)
        )
        sock.sendall(handshake.encode("ascii"))

        # Read response headers
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed during handshake")
            buf += chunk

        header_end = buf.index(b"\r\n\r\n")
        header_text = buf[:header_end].decode("ascii", errors="replace")

        if "101" not in header_text.split("\r\n")[0]:
            raise ConnectionError("WebSocket upgrade failed: %s" %
                                  header_text.split("\r\n")[0])

        # Any leftover data after headers is the start of ws frames
        self._pending = buf[header_end + 4:]
        self._sock = sock
        self._connected = True

    def send(self, text):
        """Send a text frame (masked, per RFC 6455)."""
        with self._lock:
            if not self._sock or not self._connected:
                return
            data = text.encode("utf-8")
            self._send_frame(OP_TEXT, data)

    def recv(self, timeout=0.5):
        """Read one text message. Returns str or None on timeout."""
        if not self._sock or not self._connected:
            return None
        self._sock.settimeout(timeout)
        try:
            return self._read_frame()
        except socket.timeout:
            return None
        except (OSError, ConnectionError):
            self._connected = False
            return None

    def close(self):
        """Send close frame and shut down."""
        with self._lock:
            if not self._sock:
                return
            try:
                self._send_frame(OP_CLOSE, b"")
            except OSError:
                pass
            self._connected = False
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ---- Internal ----

    def _send_frame(self, opcode, data):
        """Build and send a masked WebSocket frame."""
        frame = bytearray()
        frame.append(0x80 | opcode)  # FIN + opcode

        length = len(data)
        if length < 126:
            frame.append(0x80 | length)  # MASK bit + length
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack("!Q", length))

        # Masking key (4 random bytes)
        mask = os.urandom(4)
        frame.extend(mask)

        # Mask the payload
        masked = bytearray(length)
        for i in range(length):
            masked[i] = data[i] ^ mask[i % 4]
        frame.extend(masked)

        self._sock.sendall(bytes(frame))

    def _recv_exact(self, n):
        """Read exactly n bytes from the socket."""
        # Use any pending data from the handshake first
        buf = b""
        if hasattr(self, "_pending") and self._pending:
            if len(self._pending) >= n:
                buf = self._pending[:n]
                self._pending = self._pending[n:]
                return buf
            buf = self._pending
            self._pending = b""

        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed")
            buf += chunk
        return buf

    def _read_frame(self):
        """Read one WebSocket frame. Handles ping/pong automatically."""
        while True:
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            masked = (header[1] & 0x80) != 0
            length = header[1] & 0x7F

            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]

            mask_key = self._recv_exact(4) if masked else None

            payload = self._recv_exact(length) if length > 0 else b""

            if masked and mask_key:
                payload = bytearray(payload)
                for i in range(len(payload)):
                    payload[i] ^= mask_key[i % 4]
                payload = bytes(payload)

            if opcode == OP_TEXT:
                return payload.decode("utf-8", errors="replace")
            elif opcode == OP_PING:
                with self._lock:
                    self._send_frame(OP_PONG, payload)
            elif opcode == OP_CLOSE:
                self._connected = False
                with self._lock:
                    try:
                        self._send_frame(OP_CLOSE, b"")
                    except OSError:
                        pass
                return None
            elif opcode == OP_PONG:
                continue  # ignore pongs
            # skip unknown opcodes