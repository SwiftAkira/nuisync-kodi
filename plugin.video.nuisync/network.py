"""
network.py — Raw socket host/client for NuiSync~

Connection cascade (tries in order, falls back gracefully):
    1. UPnP auto-port-forward → direct TCP (works ~70-80% of home routers)
    2. UDP hole punching → reliable UDP transport (works ~80% of remaining)
    3. Direct IP entry → TCP (legacy Hamachi / manual port forward fallback)

Protocol: line-delimited JSON, each message terminated by newline.
Messages carry an auto-incrementing "seq" number for diagnostics.

Connection lifecycle managed by a simple state machine:
    DISCONNECTED -> CONNECTING -> CONNECTED -> RECONNECTING -> ...

All background threads check _shutdown_event frequently (≤0.5s) so the
service can exit within Kodi's 5-second kill deadline on uninstall.
"""

import socket
import threading
import json
import time

import xbmc

from nathelper import (
    discover_public_address, try_upnp_forward, encode_session,
    decode_session, udp_hole_punch, UPnPMapping,
)

# Timing constants
POLL_INTERVAL = 0.1
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 15.0

# Connection states
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"

# Transport types
TRANSPORT_TCP = "tcp"
TRANSPORT_UDP = "udp"


def _short_wait(event, seconds):
    """Wait up to `seconds`, but bail early if event is set.
    Checks every 0.5s so threads can exit fast on shutdown."""
    elapsed = 0.0
    while elapsed < seconds:
        if event.wait(min(0.5, seconds - elapsed)):
            return True  # shutdown requested
        elapsed += 0.5
    return False


class NuiSyncNetwork(object):
    """Manages the connection between host and guest."""

    def __init__(self, on_message_callback, on_status_callback,
                 auto_reconnect=True, reconnect_attempts=5,
                 reconnect_delay=3):
        self._on_message = on_message_callback
        self._on_status = on_status_callback

        # Reconnect settings
        self._auto_reconnect = auto_reconnect
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_delay = reconnect_delay

        # Connection state
        self._state = STATE_DISCONNECTED
        self._role = None
        self._remote_ip = None
        self._port = None
        self._transport = None

        # Socket handles
        self._sock = None
        self._server_sock = None
        self._udp_remote = None

        # UPnP mapping (cleaned up on shutdown)
        self._upnp_mapping = None

        # Session code for this session
        self._session_code = None

        # Threading — _shutdown_event is the master kill switch.
        # All threads check this every ≤0.5s.
        self._lock = threading.Lock()
        self._running = False
        self._shutdown_event = threading.Event()
        self._recv_thread = None
        self._heartbeat_thread = None
        self._recv_buffer = ""
        self._reconnect_cancel = threading.Event()

        # Message sequencing
        self._seq = 0

        # UDP reliability
        self._udp_pending = {}
        self._udp_acked = set()
        self._udp_lock = threading.Lock()

        # Heartbeat tracking
        self._last_pong_time = 0.0

    def _should_stop(self):
        """Check if we should stop — shutdown requested or Kodi aborting."""
        return self._shutdown_event.is_set() or xbmc.Monitor().abortRequested()

    # ------------------------------------------------------------------
    # Session code helpers
    # ------------------------------------------------------------------

    @property
    def session_code(self):
        return self._session_code

    # ------------------------------------------------------------------
    # Host
    # ------------------------------------------------------------------

    def host(self, port, use_upnp=True):
        """Host a session. Returns True on success."""
        self._role = "host"
        self._port = port
        self._state = STATE_CONNECTING

        if use_upnp:
            self._on_status("Setting up connection~")
            mapping = try_upnp_forward(port, protocol="TCP")
            if mapping:
                self._upnp_mapping = mapping
                pub_ip = mapping.get_external_ip()
                if not pub_ip:
                    result = discover_public_address()
                    pub_ip = result[0] if result else None
                if pub_ip:
                    self._session_code = encode_session(pub_ip, port)
            else:
                result = discover_public_address()
                if result:
                    self._session_code = encode_session(result[0], port)
        else:
            result = discover_public_address()
            if result:
                self._session_code = encode_session(result[0], port)

        self._transport = TRANSPORT_TCP
        return self._accept_connection(port)

    def host_direct(self, port):
        """Host without NAT traversal (legacy / Hamachi mode)."""
        self._role = "host"
        self._port = port
        self._state = STATE_CONNECTING
        self._transport = TRANSPORT_TCP
        return self._accept_connection(port)

    def _accept_connection(self, port):
        """Create server socket and wait for one client."""
        with self._lock:
            self._server_sock = socket.socket(socket.AF_INET,
                                              socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET,
                                         socket.SO_REUSEADDR, 1)
            self._server_sock.settimeout(0.5)
            try:
                self._server_sock.bind(("0.0.0.0", port))
            except OSError as exc:
                xbmc.log("[NuiSync] Bind failed: %s" % exc, xbmc.LOGERROR)
                self._on_status("Bind failed -- port %d in use?" % port)
                self._state = STATE_DISCONNECTED
                return False
            self._server_sock.listen(1)

        self._running = True

        if self._session_code:
            self._on_status("Code: %s  |  Port %d~" %
                            (self._session_code, port))
        else:
            self._on_status("Waiting for friend on port %d~" % port)

        while self._running and not self._should_stop():
            try:
                conn, addr = self._server_sock.accept()
                conn.settimeout(POLL_INTERVAL)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._lock:
                    self._sock = conn
                self._state = STATE_CONNECTED
                self._last_pong_time = time.time()
                self._on_status("Synced with %s~" % addr[0])
                self._start_recv_loop()
                self._start_heartbeat()
                return True
            except socket.timeout:
                continue
            except OSError:
                break
        return False

    # ------------------------------------------------------------------
    # Client / Guest
    # ------------------------------------------------------------------

    def join(self, host_ip, port, timeout=10):
        """Connect via direct TCP."""
        self._role = "client"
        self._remote_ip = host_ip
        self._port = port
        self._state = STATE_CONNECTING
        self._transport = TRANSPORT_TCP
        return self._do_connect(host_ip, port, timeout)

    def join_by_code(self, code, timeout=10):
        """Connect using a session code."""
        result = decode_session(code)
        if not result:
            self._on_status("Invalid session code")
            return False
        ip, port = result
        return self.join(ip, port, timeout)

    def _do_connect(self, host_ip, port, timeout=10):
        """Perform the actual TCP connect."""
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
            self._on_status("Synced with %s~" % host_ip)
            self._start_recv_loop()
            self._start_heartbeat()
            return True
        except (socket.timeout, OSError) as exc:
            xbmc.log("[NuiSync] Connection failed: %s" % exc, xbmc.LOGERROR)
            self._on_status("Couldn't connect...")
            self._state = STATE_DISCONNECTED
            try:
                sock.close()
            except OSError:
                pass
            return False

    # ------------------------------------------------------------------
    # UDP transport
    # ------------------------------------------------------------------

    def host_udp(self, port):
        """Host via UDP hole punching."""
        self._role = "host"
        self._port = port
        self._state = STATE_CONNECTING
        self._transport = TRANSPORT_UDP
        self._running = True
        pub = discover_public_address(local_port=port)
        if pub:
            self._session_code = encode_session(pub[0], pub[1])
            self._on_status("UDP Code: %s~" % self._session_code)
        else:
            self._on_status("Waiting on UDP port %d~" % port)
        return True

    def connect_udp(self, local_port, remote_ip, remote_port):
        """Connect via UDP hole punching."""
        self._on_status("Punching through NAT~")
        sock = udp_hole_punch(local_port, remote_ip, remote_port)
        if sock:
            with self._lock:
                self._sock = sock
                self._udp_remote = (remote_ip, remote_port)
            self._state = STATE_CONNECTED
            self._last_pong_time = time.time()
            self._on_status("Synced via UDP~")
            self._start_recv_loop_udp()
            self._start_heartbeat()
            return True
        else:
            self._on_status("Hole punch failed")
            self._state = STATE_DISCONNECTED
            return False

    # ------------------------------------------------------------------
    # Sending (thread-safe)
    # ------------------------------------------------------------------

    def send(self, msg_dict):
        """Send a dict as a JSON line to the peer. Thread-safe."""
        with self._lock:
            if not self._sock or self._state != STATE_CONNECTED:
                return
            self._seq += 1
            msg_dict["seq"] = self._seq
            line = json.dumps(msg_dict) + "\n"
            raw = line.encode("utf-8")
            try:
                if self._transport == TRANSPORT_UDP:
                    if self._udp_remote:
                        self._sock.sendto(raw, self._udp_remote)
                        cmd = msg_dict.get("cmd", "")
                        if cmd not in ("sync", "pong", "ping", "buffering"):
                            with self._udp_lock:
                                self._udp_pending[self._seq] = (
                                    raw, time.time(), 0)
                else:
                    self._sock.sendall(raw)
            except OSError as exc:
                xbmc.log("[NuiSync] Send error: %s" % exc, xbmc.LOGERROR)
                threading.Thread(target=self._handle_disconnect).start()

    def _send_udp_ack(self, seq):
        ack = json.dumps({"cmd": "_ack", "ack_seq": seq}) + "\n"
        with self._lock:
            if self._sock and self._udp_remote:
                try:
                    self._sock.sendto(ack.encode("utf-8"), self._udp_remote)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Receiving — TCP
    # ------------------------------------------------------------------

    def _start_recv_loop(self):
        self._recv_buffer = ""
        self._recv_thread = threading.Thread(target=self._recv_loop,
                                             name="NuiSyncRecv")
        self._recv_thread.start()

    def _recv_loop(self):
        while self._running and not self._should_stop():
            try:
                with self._lock:
                    sock = self._sock
                if not sock:
                    break
                chunk = sock.recv(4096)
                if not chunk:
                    self._handle_disconnect()
                    return
                self._recv_buffer += chunk.decode("utf-8")
                self._process_buffer()
            except socket.timeout:
                continue
            except OSError:
                self._handle_disconnect()
                return

    # ------------------------------------------------------------------
    # Receiving — UDP
    # ------------------------------------------------------------------

    def _start_recv_loop_udp(self):
        self._recv_buffer = ""
        self._recv_thread = threading.Thread(target=self._recv_loop_udp,
                                             name="NuiSyncRecvUDP")
        self._recv_thread.start()
        t = threading.Thread(target=self._udp_retransmit_loop,
                             name="NuiSyncRetransmit")
        t.start()

    def _recv_loop_udp(self):
        with self._lock:
            sock = self._sock
        if not sock:
            return
        sock.settimeout(POLL_INTERVAL)
        while self._running and not self._should_stop():
            try:
                data, addr = sock.recvfrom(8192)
                if not data:
                    continue
                text = data.decode("utf-8", errors="replace")
                for line in text.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except (ValueError, KeyError):
                        continue
                    self._dispatch_message(msg)
            except socket.timeout:
                continue
            except OSError:
                self._handle_disconnect()
                return

    def _udp_retransmit_loop(self):
        max_retries = 5
        while self._running and not self._should_stop():
            now = time.time()
            with self._udp_lock:
                to_remove = []
                for seq, (raw, sent_time, retries) in self._udp_pending.items():
                    age = now - sent_time
                    delay = 0.5 * (2 ** retries)
                    if age >= delay:
                        if retries >= max_retries:
                            to_remove.append(seq)
                            continue
                        with self._lock:
                            if self._sock and self._udp_remote:
                                try:
                                    self._sock.sendto(raw, self._udp_remote)
                                except OSError:
                                    pass
                        self._udp_pending[seq] = (raw, now, retries + 1)
                for seq in to_remove:
                    del self._udp_pending[seq]
            if _short_wait(self._shutdown_event, 0.3):
                break

    # ------------------------------------------------------------------
    # Shared message processing
    # ------------------------------------------------------------------

    def _process_buffer(self):
        while "\n" in self._recv_buffer:
            line, self._recv_buffer = self._recv_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, KeyError):
                continue
            self._dispatch_message(msg)

    def _dispatch_message(self, msg):
        cmd = msg.get("cmd", "")
        if cmd == "ping":
            self.send({"cmd": "pong"})
            return
        elif cmd == "pong":
            self._last_pong_time = time.time()
            return
        if cmd == "_ack":
            ack_seq = msg.get("ack_seq")
            if ack_seq is not None:
                with self._udp_lock:
                    self._udp_pending.pop(ack_seq, None)
            return
        if self._transport == TRANSPORT_UDP:
            seq = msg.get("seq")
            if seq is not None and cmd not in ("sync", "ping", "pong",
                                                "buffering"):
                with self._udp_lock:
                    if seq in self._udp_acked:
                        self._send_udp_ack(seq)
                        return
                    self._udp_acked.add(seq)
                    if len(self._udp_acked) > 200:
                        cutoff = max(self._udp_acked) - 150
                        self._udp_acked = {s for s in self._udp_acked
                                           if s > cutoff}
                self._send_udp_ack(seq)
        try:
            self._on_message(msg)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Heartbeat — uses short waits so it exits fast on shutdown
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
                elapsed = time.time() - self._last_pong_time
                if elapsed > HEARTBEAT_TIMEOUT:
                    xbmc.log("[NuiSync] Heartbeat timeout (%.0fs)" %
                             elapsed, xbmc.LOGWARNING)
                    self._handle_disconnect()
                    return
            # Wait in 0.5s chunks instead of one long 5s block
            if _short_wait(self._shutdown_event, HEARTBEAT_INTERVAL):
                break

    # ------------------------------------------------------------------
    # Disconnect and reconnect
    # ------------------------------------------------------------------

    def _handle_disconnect(self):
        if self._state in (STATE_DISCONNECTED, STATE_RECONNECTING):
            return
        if self._should_stop():
            self._state = STATE_DISCONNECTED
            return

        xbmc.log("[NuiSync] Friend disconnected", xbmc.LOGINFO)

        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

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

    def _attempt_reconnect(self):
        for attempt in range(1, self._reconnect_attempts + 1):
            if self._reconnect_cancel.is_set() or not self._running:
                break
            if self._should_stop():
                break

            self._on_status("Reconnecting (%d/%d)~" %
                            (attempt, self._reconnect_attempts))

            success = False
            if self._role == "host":
                success = self._accept_connection(self._port)
            elif self._role == "client":
                success = self._do_connect(self._remote_ip, self._port)

            if success:
                self._on_status("Back in sync~")
                if self._role == "client":
                    self.send({"cmd": "state_request"})
                return

            # Wait between attempts using short chunks
            if _short_wait(self._reconnect_cancel, self._reconnect_delay):
                break
            if self._should_stop():
                break

        self._state = STATE_DISCONNECTED
        self._running = False
        self._on_status("Disconnected")

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def connected(self):
        return self._state == STATE_CONNECTED

    @property
    def transport(self):
        return self._transport

    # ------------------------------------------------------------------
    # Shutdown — signals all threads to exit within 0.5s
    # ------------------------------------------------------------------

    def shutdown(self):
        """Tear everything down. All threads exit within ~0.5s."""
        self._running = False
        self._shutdown_event.set()
        self._reconnect_cancel.set()
        self._state = STATE_DISCONNECTED

        with self._lock:
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

        # Clean up UPnP port mapping
        if self._upnp_mapping:
            try:
                self._upnp_mapping.teardown()
            except Exception:
                pass
            self._upnp_mapping = None

        # Clear UDP state
        with self._udp_lock:
            self._udp_pending.clear()
            self._udp_acked.clear()

        self._on_status("Disconnected")