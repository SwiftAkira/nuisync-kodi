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

Includes:
    - Heartbeat (ping/pong every 5s, dead after 15s without pong)
    - Auto-reconnect with configurable attempts and delay
    - Thread-safe sends via lock
    - Ping/pong handled at network layer, not forwarded to player
    - UPnP mapping cleanup on shutdown
    - Session code generation for serverless discovery
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


class NuiSyncNetwork(object):
    """Manages the connection between host and guest.

    Supports two transports:
        - TCP (direct or via UPnP port forward) — full reliability
        - UDP (via hole punching) — lightweight reliability layer for commands
    """

    def __init__(self, on_message_callback, on_status_callback,
                 auto_reconnect=True, reconnect_attempts=5,
                 reconnect_delay=3):
        """
        Args:
            on_message_callback: callable(dict) -- called when a complete
                                 message arrives (not ping/pong).
            on_status_callback:  callable(str)  -- called with status text.
            auto_reconnect:      Whether to auto-reconnect on disconnect.
            reconnect_attempts:  Max reconnect attempts before giving up.
            reconnect_delay:     Seconds between reconnect attempts.
        """
        self._on_message = on_message_callback
        self._on_status = on_status_callback

        # Reconnect settings
        self._auto_reconnect = auto_reconnect
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_delay = reconnect_delay

        # Connection state
        self._state = STATE_DISCONNECTED
        self._role = None           # "host" or "client"
        self._remote_ip = None      # client stores host IP
        self._port = None
        self._transport = None      # TRANSPORT_TCP or TRANSPORT_UDP

        # Socket handles
        self._sock = None           # the connected peer socket (TCP or UDP)
        self._server_sock = None    # only set on host side (TCP)
        self._udp_remote = None     # (ip, port) for UDP peer

        # UPnP mapping (cleaned up on shutdown)
        self._upnp_mapping = None

        # Session code for this session
        self._session_code = None

        # Threading
        self._lock = threading.Lock()          # guards _sock and sends
        self._running = False
        self._recv_thread = None
        self._heartbeat_thread = None
        self._recv_buffer = ""
        self._reconnect_cancel = threading.Event()

        # Message sequencing (diagnostic + UDP reliability)
        self._seq = 0

        # UDP reliability: track unacked messages for retransmit
        self._udp_pending = {}      # seq -> (msg_bytes, send_time, retries)
        self._udp_acked = set()     # seqs we've received and acked
        self._udp_lock = threading.Lock()

        # Heartbeat tracking
        self._last_pong_time = 0.0

    # ------------------------------------------------------------------
    # Session code helpers
    # ------------------------------------------------------------------

    @property
    def session_code(self):
        """The session code for this hosting session, or None."""
        return self._session_code

    # ------------------------------------------------------------------
    # Host — with connection cascade
    # ------------------------------------------------------------------

    def host(self, port, use_upnp=True):
        """Host a session using the connection cascade.

        1. Try UPnP auto-forward → TCP listen
        2. Discover public address for session code
        3. Wait for peer connection

        Returns True on success.
        """
        self._role = "host"
        self._port = port
        self._state = STATE_CONNECTING

        # Step 1: Try UPnP port forward
        if use_upnp:
            self._on_status("Setting up connection~")
            xbmc.log("[NuiSync] Trying UPnP port forward...", xbmc.LOGINFO)
            mapping = try_upnp_forward(port, protocol="TCP")
            if mapping:
                self._upnp_mapping = mapping
                xbmc.log("[NuiSync] UPnP succeeded!", xbmc.LOGINFO)

                # Get public IP: try UPnP first, fall back to STUN
                pub_ip = mapping.get_external_ip()
                if not pub_ip:
                    result = discover_public_address()
                    pub_ip = result[0] if result else None

                if pub_ip:
                    self._session_code = encode_session(pub_ip, port)
                    xbmc.log("[NuiSync] Session code: %s" %
                             self._session_code, xbmc.LOGINFO)
            else:
                xbmc.log("[NuiSync] UPnP not available, using direct mode",
                         xbmc.LOGINFO)
                # Still try to get a session code via STUN (may work if
                # user has manual port forward or is on a public IP)
                result = discover_public_address()
                if result:
                    self._session_code = encode_session(result[0], port)
        else:
            result = discover_public_address()
            if result:
                self._session_code = encode_session(result[0], port)

        # Step 2: TCP listen (works whether UPnP succeeded or not)
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
            self._server_sock.settimeout(1.0)
            try:
                self._server_sock.bind(("0.0.0.0", port))
            except OSError as exc:
                xbmc.log("[NuiSync] Bind failed: %s" % exc,
                         xbmc.LOGERROR)
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

        xbmc.log("[NuiSync] Hosting on 0.0.0.0:%d" % port, xbmc.LOGINFO)

        while self._running:
            try:
                conn, addr = self._server_sock.accept()
                conn.settimeout(POLL_INTERVAL)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._lock:
                    self._sock = conn
                self._state = STATE_CONNECTED
                self._last_pong_time = time.time()
                xbmc.log("[NuiSync] Friend connected from %s" % str(addr),
                         xbmc.LOGINFO)
                self._on_status("Synced with %s~" % addr[0])
                self._start_recv_loop()
                self._start_heartbeat()
                return True
            except socket.timeout:
                if xbmc.Monitor().abortRequested():
                    self.shutdown()
                    return False
                continue
            except OSError:
                break
        return False

    # ------------------------------------------------------------------
    # Client / Guest — with connection cascade
    # ------------------------------------------------------------------

    def join(self, host_ip, port, timeout=10):
        """Connect to the host via direct TCP. Returns True on success."""
        self._role = "client"
        self._remote_ip = host_ip
        self._port = port
        self._state = STATE_CONNECTING
        self._transport = TRANSPORT_TCP

        return self._do_connect(host_ip, port, timeout)

    def join_by_code(self, code, timeout=10):
        """Connect to a host using a session code.

        Decodes the code to IP:port and connects via TCP.
        """
        result = decode_session(code)
        if not result:
            self._on_status("Invalid session code")
            return False

        ip, port = result
        xbmc.log("[NuiSync] Session code decoded: %s:%d" % (ip, port),
                 xbmc.LOGINFO)
        return self.join(ip, port, timeout)

    def _do_connect(self, host_ip, port, timeout=10):
        """Perform the actual TCP connect."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        self._on_status("Connecting to %s:%d~" % (host_ip, port))
        xbmc.log("[NuiSync] Connecting to %s:%d" % (host_ip, port),
                 xbmc.LOGINFO)
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
            xbmc.log("[NuiSync] Connection failed: %s" % exc,
                     xbmc.LOGERROR)
            self._on_status("Couldn't connect...")
            self._state = STATE_DISCONNECTED
            try:
                sock.close()
            except OSError:
                pass
            return False

    # ------------------------------------------------------------------
    # UDP transport (for hole-punched connections)
    # ------------------------------------------------------------------

    def host_udp(self, port):
        """Host via UDP hole punching. Requires both peers to have
        exchanged session codes out-of-band.

        Returns True after a peer connects via hole punch.
        """
        self._role = "host"
        self._port = port
        self._state = STATE_CONNECTING
        self._transport = TRANSPORT_UDP
        self._running = True

        # Discover our public address
        pub = discover_public_address(local_port=port)
        if pub:
            self._session_code = encode_session(pub[0], pub[1])
            self._on_status("UDP Code: %s~" % self._session_code)
        else:
            self._on_status("Waiting on UDP port %d~" % port)

        return True  # actual connection happens when peer code is entered

    def connect_udp(self, local_port, remote_ip, remote_port):
        """Connect to peer via UDP hole punching."""
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
    # Sending (thread-safe, transport-aware)
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
                        # Track for retransmit if it's a command (not sync/pong)
                        cmd = msg_dict.get("cmd", "")
                        if cmd not in ("sync", "pong", "ping", "buffering"):
                            with self._udp_lock:
                                self._udp_pending[self._seq] = (
                                    raw, time.time(), 0)
                else:
                    self._sock.sendall(raw)
            except OSError as exc:
                xbmc.log("[NuiSync] Send error: %s" % exc, xbmc.LOGERROR)
                threading.Thread(target=self._handle_disconnect,
                                 daemon=True).start()

    def _send_udp_ack(self, seq):
        """Send an ACK for a received UDP message."""
        ack = json.dumps({"cmd": "_ack", "ack_seq": seq}) + "\n"
        with self._lock:
            if self._sock and self._udp_remote:
                try:
                    self._sock.sendto(ack.encode("utf-8"), self._udp_remote)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Receiving — TCP (background thread)
    # ------------------------------------------------------------------

    def _start_recv_loop(self):
        self._recv_buffer = ""
        self._recv_thread = threading.Thread(target=self._recv_loop,
                                             name="NuiSyncRecv")
        self._recv_thread.daemon = True
        self._recv_thread.start()

    def _recv_loop(self):
        """Read from TCP socket, dispatch messages. Ping/pong handled here."""
        monitor = xbmc.Monitor()
        while self._running and not monitor.abortRequested():
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
    # Receiving — UDP (background thread)
    # ------------------------------------------------------------------

    def _start_recv_loop_udp(self):
        self._recv_buffer = ""
        self._recv_thread = threading.Thread(target=self._recv_loop_udp,
                                             name="NuiSyncRecvUDP")
        self._recv_thread.daemon = True
        self._recv_thread.start()

        # Start retransmit thread for unacked messages
        t = threading.Thread(target=self._udp_retransmit_loop,
                             name="NuiSyncRetransmit")
        t.daemon = True
        t.start()

    def _recv_loop_udp(self):
        """Read from UDP socket, dispatch messages."""
        monitor = xbmc.Monitor()
        with self._lock:
            sock = self._sock
        if not sock:
            return
        sock.settimeout(POLL_INTERVAL)

        while self._running and not monitor.abortRequested():
            try:
                data, addr = sock.recvfrom(8192)
                if not data:
                    continue
                text = data.decode("utf-8", errors="replace")
                # UDP messages are individual lines (no buffering needed)
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
        """Retransmit unacked UDP messages with exponential backoff."""
        monitor = xbmc.Monitor()
        max_retries = 5
        while self._running and not monitor.abortRequested():
            now = time.time()
            with self._udp_lock:
                to_remove = []
                for seq, (raw, sent_time, retries) in self._udp_pending.items():
                    age = now - sent_time
                    # Backoff: 0.5s, 1s, 2s, 4s, 8s
                    delay = 0.5 * (2 ** retries)
                    if age >= delay:
                        if retries >= max_retries:
                            to_remove.append(seq)
                            continue
                        # Retransmit
                        with self._lock:
                            if self._sock and self._udp_remote:
                                try:
                                    self._sock.sendto(raw, self._udp_remote)
                                except OSError:
                                    pass
                        self._udp_pending[seq] = (raw, now, retries + 1)
                for seq in to_remove:
                    del self._udp_pending[seq]

            if monitor.waitForAbort(0.3):
                break

    # ------------------------------------------------------------------
    # Shared message processing
    # ------------------------------------------------------------------

    def _process_buffer(self):
        """Extract complete JSON lines from the receive buffer."""
        while "\n" in self._recv_buffer:
            line, self._recv_buffer = self._recv_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, KeyError):
                xbmc.log("[NuiSync] Bad JSON: %s" % line[:100],
                         xbmc.LOGWARNING)
                continue
            self._dispatch_message(msg)

    def _dispatch_message(self, msg):
        """Route a parsed message to the right handler."""
        cmd = msg.get("cmd", "")

        # Handle ping/pong at network layer
        if cmd == "ping":
            self.send({"cmd": "pong"})
            return
        elif cmd == "pong":
            self._last_pong_time = time.time()
            return

        # Handle UDP ACKs
        if cmd == "_ack":
            ack_seq = msg.get("ack_seq")
            if ack_seq is not None:
                with self._udp_lock:
                    self._udp_pending.pop(ack_seq, None)
            return

        # For UDP: send ACK for command messages
        if self._transport == TRANSPORT_UDP:
            seq = msg.get("seq")
            if seq is not None and cmd not in ("sync", "ping", "pong",
                                                "buffering"):
                # Dedup: skip if already processed
                with self._udp_lock:
                    if seq in self._udp_acked:
                        self._send_udp_ack(seq)
                        return
                    self._udp_acked.add(seq)
                    # Trim old acked seqs to prevent unbounded growth
                    if len(self._udp_acked) > 200:
                        cutoff = max(self._udp_acked) - 150
                        self._udp_acked = {s for s in self._udp_acked
                                           if s > cutoff}
                self._send_udp_ack(seq)

        # Forward everything else to the player layer
        self._on_message(msg)

    # ------------------------------------------------------------------
    # Heartbeat (background thread)
    # ------------------------------------------------------------------

    def _start_heartbeat(self):
        self._last_pong_time = time.time()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                  name="NuiSyncHeartbeat")
        self._heartbeat_thread.daemon = True
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        """Send ping every HEARTBEAT_INTERVAL. Declare dead if no pong
        within HEARTBEAT_TIMEOUT."""
        monitor = xbmc.Monitor()
        while self._running and not monitor.abortRequested():
            if self._state == STATE_CONNECTED:
                self.send({"cmd": "ping"})
                elapsed = time.time() - self._last_pong_time
                if elapsed > HEARTBEAT_TIMEOUT:
                    xbmc.log("[NuiSync] Heartbeat timeout (%.0fs)" %
                             elapsed, xbmc.LOGWARNING)
                    self._handle_disconnect()
                    return
            if monitor.waitForAbort(HEARTBEAT_INTERVAL):
                break

    # ------------------------------------------------------------------
    # Disconnect and reconnect
    # ------------------------------------------------------------------

    def _handle_disconnect(self):
        """Called when the peer is lost. Attempt reconnect if enabled."""
        if self._state in (STATE_DISCONNECTED, STATE_RECONNECTING):
            return

        xbmc.log("[NuiSync] Friend disconnected", xbmc.LOGINFO)

        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

        if self._auto_reconnect and self._running:
            self._state = STATE_RECONNECTING
            self._on_status("Reconnecting~")
            self._reconnect_cancel.clear()
            t = threading.Thread(target=self._attempt_reconnect,
                                 name="NuiSyncReconnect")
            t.daemon = True
            t.start()
        else:
            self._state = STATE_DISCONNECTED
            self._running = False
            self._on_status("Disconnected")

    def _attempt_reconnect(self):
        """Try to re-establish the connection."""
        for attempt in range(1, self._reconnect_attempts + 1):
            if self._reconnect_cancel.is_set() or not self._running:
                break
            if xbmc.Monitor().abortRequested():
                break

            self._on_status("Reconnecting (%d/%d)~" %
                            (attempt, self._reconnect_attempts))
            xbmc.log("[NuiSync] Reconnect attempt %d/%d" %
                     (attempt, self._reconnect_attempts), xbmc.LOGINFO)

            success = False
            if self._role == "host":
                success = self._accept_connection(self._port)
            elif self._role == "client":
                success = self._do_connect(self._remote_ip, self._port)

            if success:
                xbmc.log("[NuiSync] Reconnected!", xbmc.LOGINFO)
                self._on_status("Back in sync~")
                if self._role == "client":
                    self.send({"cmd": "state_request"})
                return

            if self._reconnect_cancel.wait(self._reconnect_delay):
                break

        xbmc.log("[NuiSync] Reconnection failed after %d attempts" %
                 self._reconnect_attempts, xbmc.LOGERROR)
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
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Tear everything down cleanly, including UPnP mappings."""
        self._running = False
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
            self._upnp_mapping.teardown()
            self._upnp_mapping = None

        # Clear UDP state
        with self._udp_lock:
            self._udp_pending.clear()
            self._udp_acked.clear()

        self._on_status("Disconnected")