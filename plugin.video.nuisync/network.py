"""
network.py — WebSocket relay network for NuiSync~

Connects to a Cloudflare Worker relay via WebSocket. Both host and
guest connect outbound — no port forwarding, no UPnP, no STUN needed.
Works through any NAT, firewall, or CGNAT.

Protocol: JSON messages relayed through the Worker. The Worker pairs
two clients in a room identified by a short code.

Also supports direct TCP for LAN/Hamachi fallback.
"""

import socket
import threading
import json
import time
import random
import string

import xbmc

from wsclient import WebSocketClient

# Connection states
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"

# Relay URL — set after deploying the Cloudflare Worker
RELAY_URL = "wss://nuisync-relay.swiftakira.workers.dev"

# Room code charset (no confusing chars)
CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Timing
HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_TIMEOUT = 30.0
POLL_INTERVAL = 0.1


def generate_room_code(length=5):
    """Generate a random room code."""
    return "".join(random.choice(CODE_CHARS) for _ in range(length))


def _short_wait(event, seconds):
    """Wait up to `seconds`, bail early if event is set."""
    elapsed = 0.0
    while elapsed < seconds:
        if event.wait(min(0.5, seconds - elapsed)):
            return True
        elapsed += 0.5
    return False


class NuiSyncNetwork(object):
    """Manages the relay connection between host and guest."""

    def __init__(self, on_message_callback, on_status_callback,
                 auto_reconnect=True, reconnect_attempts=5,
                 reconnect_delay=3):
        self._on_message = on_message_callback
        self._on_status = on_status_callback

        self._auto_reconnect = auto_reconnect
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_delay = reconnect_delay

        self._state = STATE_DISCONNECTED
        self._role = None
        self._room_code = None
        self._ws = None

        # Direct TCP fallback
        self._sock = None
        self._server_sock = None
        self._transport = None  # "relay" or "tcp"
        self._remote_ip = None
        self._port = None

        self._lock = threading.Lock()
        self._running = False
        self._shutdown_event = threading.Event()
        self._recv_thread = None
        self._heartbeat_thread = None
        self._reconnect_cancel = threading.Event()
        self._recv_buffer = ""

        self._seq = 0
        self._last_pong_time = 0.0
        self._peer_connected = False

    def _should_stop(self):
        return self._shutdown_event.is_set() or xbmc.Monitor().abortRequested()

    # ------------------------------------------------------------------
    # Relay mode (Cloudflare Worker)
    # ------------------------------------------------------------------

    def host(self, room_code=None, **kwargs):
        """Host a session via the relay. Returns True when peer joins."""
        self._role = "host"
        self._room_code = room_code or generate_room_code()
        self._transport = "relay"
        self._state = STATE_CONNECTING

        url = "%s/room/%s" % (RELAY_URL, self._room_code)
        self._on_status("Code: %s" % self._room_code)

        ws = WebSocketClient(url, timeout=15)
        try:
            ws.connect()
        except Exception as exc:
            xbmc.log("[NuiSync] Relay connect failed: %s" % exc,
                     xbmc.LOGERROR)
            self._on_status("Couldn't reach relay server")
            self._state = STATE_DISCONNECTED
            return False

        with self._lock:
            self._ws = ws

        self._running = True
        self._last_pong_time = time.time()
        self._on_status("Code: %s  Waiting for friend~" % self._room_code)

        # Wait for peer to join
        while self._running and not self._should_stop():
            msg_text = ws.recv(timeout=0.5)
            if msg_text is None:
                if not ws.connected:
                    self._on_status("Relay disconnected")
                    self._state = STATE_DISCONNECTED
                    return False
                continue
            try:
                msg = json.loads(msg_text)
            except (ValueError, KeyError):
                continue
            cmd = msg.get("cmd", "")
            if cmd == "_role":
                continue  # our own role assignment
            if cmd == "_peer_joined":
                self._peer_connected = True
                self._state = STATE_CONNECTED
                self._start_recv_loop_relay()
                self._start_heartbeat()
                return True

        self._state = STATE_DISCONNECTED
        return False

    def join(self, room_code, **kwargs):
        """Join a session via the relay."""
        self._role = "client"
        self._room_code = room_code.strip().upper().replace("-", "")
        self._transport = "relay"
        self._state = STATE_CONNECTING

        url = "%s/room/%s" % (RELAY_URL, self._room_code)
        self._on_status("Connecting~")

        ws = WebSocketClient(url, timeout=15)
        try:
            ws.connect()
        except Exception as exc:
            xbmc.log("[NuiSync] Relay connect failed: %s" % exc,
                     xbmc.LOGERROR)
            self._on_status("Couldn't reach relay server")
            self._state = STATE_DISCONNECTED
            return False

        with self._lock:
            self._ws = ws

        self._running = True
        self._peer_connected = True
        self._state = STATE_CONNECTED
        self._last_pong_time = time.time()
        self._on_status("Connected~")
        self._start_recv_loop_relay()
        self._start_heartbeat()
        return True

    # ------------------------------------------------------------------
    # Direct TCP fallback (LAN / Hamachi)
    # ------------------------------------------------------------------

    def host_direct(self, port):
        """Host via direct TCP (LAN/Hamachi)."""
        self._role = "host"
        self._port = port
        self._transport = "tcp"
        self._state = STATE_CONNECTING
        return self._accept_connection(port)

    def join_direct(self, host_ip, port, timeout=10):
        """Join via direct TCP (LAN/Hamachi)."""
        self._role = "client"
        self._remote_ip = host_ip
        self._port = port
        self._transport = "tcp"
        self._state = STATE_CONNECTING
        return self._do_connect(host_ip, port, timeout)

    def _accept_connection(self, port):
        with self._lock:
            self._server_sock = socket.socket(socket.AF_INET,
                                              socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET,
                                         socket.SO_REUSEADDR, 1)
            self._server_sock.settimeout(0.5)
            try:
                self._server_sock.bind(("0.0.0.0", port))
            except OSError as exc:
                self._on_status("Bind failed -- port %d in use?" % port)
                self._state = STATE_DISCONNECTED
                return False
            self._server_sock.listen(1)

        self._running = True
        self._on_status("Waiting on port %d~" % port)

        while self._running and not self._should_stop():
            try:
                conn, addr = self._server_sock.accept()
                conn.settimeout(POLL_INTERVAL)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._lock:
                    self._sock = conn
                self._state = STATE_CONNECTED
                self._last_pong_time = time.time()
                self._on_status("Connected to %s~" % addr[0])
                self._start_recv_loop_tcp()
                self._start_heartbeat()
                return True
            except socket.timeout:
                continue
            except OSError:
                break
        return False

    def _do_connect(self, host_ip, port, timeout=10):
        if self._should_stop():
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        self._on_status("Connecting to %s:%d~" % (host_ip, port))
        try:
            sock.connect((host_ip, port))
            sock.settimeout(POLL_INTERVAL)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._sock = sock
            self._running = True
            self._state = STATE_CONNECTED
            self._last_pong_time = time.time()
            self._on_status("Connected to %s~" % host_ip)
            self._start_recv_loop_tcp()
            self._start_heartbeat()
            return True
        except (socket.timeout, OSError) as exc:
            self._on_status("Couldn't connect...")
            self._state = STATE_DISCONNECTED
            try:
                sock.close()
            except OSError:
                pass
            return False

    # ------------------------------------------------------------------
    # Send (works for both relay and TCP)
    # ------------------------------------------------------------------

    def send(self, msg_dict):
        with self._lock:
            if self._state != STATE_CONNECTED:
                return
            self._seq += 1
            msg_dict["seq"] = self._seq
            line = json.dumps(msg_dict)

            try:
                if self._transport == "relay" and self._ws:
                    self._ws.send(line)
                elif self._sock:
                    self._sock.sendall((line + "\n").encode("utf-8"))
            except OSError as exc:
                xbmc.log("[NuiSync] Send error: %s" % exc, xbmc.LOGERROR)
                threading.Thread(target=self._handle_disconnect).start()

    # ------------------------------------------------------------------
    # Receive — Relay (WebSocket)
    # ------------------------------------------------------------------

    def _start_recv_loop_relay(self):
        self._recv_thread = threading.Thread(target=self._recv_loop_relay,
                                             name="NuiSyncRecv")
        self._recv_thread.start()

    def _recv_loop_relay(self):
        while self._running and not self._should_stop():
            with self._lock:
                ws = self._ws
            if not ws or not ws.connected:
                self._handle_disconnect()
                return

            msg_text = ws.recv(timeout=0.5)
            if msg_text is None:
                if ws and not ws.connected:
                    self._handle_disconnect()
                    return
                continue

            try:
                msg = json.loads(msg_text)
            except (ValueError, KeyError):
                continue

            cmd = msg.get("cmd", "")
            # Handle relay control messages
            if cmd == "_peer_left":
                xbmc.log("[NuiSync] Peer left via relay", xbmc.LOGINFO)
                self._handle_disconnect()
                return
            if cmd == "_peer_joined":
                self._peer_connected = True
                continue
            if cmd == "_role":
                continue

            # Handle app-level ping/pong
            if cmd == "ping":
                self.send({"cmd": "pong"})
                continue
            if cmd == "pong":
                self._last_pong_time = time.time()
                continue

            # Forward to player
            try:
                self._on_message(msg)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Receive — TCP
    # ------------------------------------------------------------------

    def _start_recv_loop_tcp(self):
        self._recv_buffer = ""
        self._recv_thread = threading.Thread(target=self._recv_loop_tcp,
                                             name="NuiSyncRecv")
        self._recv_thread.start()

    def _recv_loop_tcp(self):
        while self._running and not self._should_stop():
            with self._lock:
                sock = self._sock
            if not sock:
                break
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    self._handle_disconnect()
                    return
                self._recv_buffer += chunk.decode("utf-8")
                while "\n" in self._recv_buffer:
                    line, self._recv_buffer = self._recv_buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except (ValueError, KeyError):
                        continue
                    cmd = msg.get("cmd", "")
                    if cmd == "ping":
                        self.send({"cmd": "pong"})
                    elif cmd == "pong":
                        self._last_pong_time = time.time()
                    else:
                        try:
                            self._on_message(msg)
                        except Exception:
                            pass
            except socket.timeout:
                continue
            except OSError:
                self._handle_disconnect()
                return

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _start_heartbeat(self):
        self._last_pong_time = time.time()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                  name="NuiSyncHeartbeat")
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while self._running and not self._should_stop():
            if self._state == STATE_CONNECTED:
                self.send({"cmd": "ping"})
                if time.time() - self._last_pong_time > HEARTBEAT_TIMEOUT:
                    xbmc.log("[NuiSync] Heartbeat timeout", xbmc.LOGWARNING)
                    self._handle_disconnect()
                    return
            if _short_wait(self._shutdown_event, HEARTBEAT_INTERVAL):
                break

    # ------------------------------------------------------------------
    # Disconnect / reconnect
    # ------------------------------------------------------------------

    def _handle_disconnect(self):
        if self._state in (STATE_DISCONNECTED, STATE_RECONNECTING):
            return
        if self._should_stop():
            self._state = STATE_DISCONNECTED
            return

        xbmc.log("[NuiSync] Disconnected", xbmc.LOGINFO)
        self._close_connections()

        if self._auto_reconnect and self._running and not self._should_stop():
            self._state = STATE_RECONNECTING
            self._on_status("Reconnecting~")
            self._reconnect_cancel.clear()
            t = threading.Thread(target=self._attempt_reconnect,
                                 name="NuiSyncReconnect")
            t.start()
        else:
            self._state = STATE_DISCONNECTED
            self._running = False
            self._on_status("Disconnected")

    def _close_connections(self):
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
            for s in (self._sock, self._server_sock):
                if s:
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        s.close()
                    except OSError:
                        pass
            self._sock = None
            self._server_sock = None
        self._peer_connected = False

    def _attempt_reconnect(self):
        for attempt in range(1, self._reconnect_attempts + 1):
            if self._reconnect_cancel.is_set() or not self._running:
                break
            if self._should_stop():
                break

            self._on_status("Reconnecting (%d/%d)~" %
                            (attempt, self._reconnect_attempts))

            success = False
            if self._transport == "relay" and self._room_code:
                if self._role == "host":
                    # Re-host the same room (peer will rejoin)
                    success = self._reconnect_relay()
                else:
                    success = self.join(self._room_code)
            elif self._transport == "tcp":
                if self._role == "host":
                    success = self._accept_connection(self._port)
                else:
                    success = self._do_connect(self._remote_ip, self._port)

            if success:
                self._on_status("Back in sync~")
                if self._role == "client":
                    self.send({"cmd": "state_request"})
                return

            if _short_wait(self._reconnect_cancel, self._reconnect_delay):
                break
            if self._should_stop():
                break

        self._state = STATE_DISCONNECTED
        self._running = False
        self._on_status("Disconnected")

    def _reconnect_relay(self):
        """Reconnect to relay room as host (skip waiting for peer)."""
        url = "%s/room/%s" % (RELAY_URL, self._room_code)
        ws = WebSocketClient(url, timeout=10)
        try:
            ws.connect()
        except Exception:
            return False
        with self._lock:
            self._ws = ws
        self._running = True
        self._state = STATE_CONNECTED
        self._last_pong_time = time.time()
        self._start_recv_loop_relay()
        self._start_heartbeat()
        return True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def connected(self):
        return self._state == STATE_CONNECTED

    @property
    def session_code(self):
        return self._room_code

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        self._running = False
        self._shutdown_event.set()
        self._reconnect_cancel.set()
        self._state = STATE_DISCONNECTED
        self._close_connections()
        self._on_status("Disconnected")