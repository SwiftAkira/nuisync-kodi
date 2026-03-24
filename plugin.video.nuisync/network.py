"""
network.py — Raw socket host/client for NuiSync~

Host binds on 0.0.0.0:<port> and accepts exactly one peer.
Client connects to the host's Hamachi IP.

Protocol: line-delimited JSON, each message terminated by newline.
Messages carry an auto-incrementing "seq" number for diagnostics.

Connection lifecycle managed by a simple state machine:
    DISCONNECTED -> CONNECTING -> CONNECTED -> RECONNECTING -> ...

Includes:
    - Heartbeat (ping/pong every 5s, dead after 15s without pong)
    - Auto-reconnect with configurable attempts and delay
    - Thread-safe sends via lock
    - Ping/pong handled at network layer, not forwarded to player
"""

import socket
import threading
import json
import time

import xbmc

# Timing constants
POLL_INTERVAL = 0.1
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 15.0

# Connection states
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"


class NuiSyncNetwork(object):
    """Manages the TCP connection between host and guest."""

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

        # Socket handles
        self._sock = None           # the connected peer socket
        self._server_sock = None    # only set on host side

        # Threading
        self._lock = threading.Lock()          # guards _sock and sends
        self._running = False
        self._recv_thread = None
        self._heartbeat_thread = None
        self._recv_buffer = ""
        self._reconnect_cancel = threading.Event()

        # Message sequencing (diagnostic)
        self._seq = 0

        # Heartbeat tracking
        self._last_pong_time = 0.0

    # ------------------------------------------------------------------
    # Host
    # ------------------------------------------------------------------

    def host(self, port):
        """Bind and listen. Blocks until a peer connects or shutdown."""
        self._role = "host"
        self._port = port
        self._state = STATE_CONNECTING

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
    # Client / Guest
    # ------------------------------------------------------------------

    def join(self, host_ip, port, timeout=10):
        """Connect to the host. Returns True on success."""
        self._role = "client"
        self._remote_ip = host_ip
        self._port = port
        self._state = STATE_CONNECTING

        return self._do_connect(host_ip, port, timeout)

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
            try:
                self._sock.sendall(line.encode("utf-8"))
            except OSError as exc:
                xbmc.log("[NuiSync] Send error: %s" % exc, xbmc.LOGERROR)
                threading.Thread(target=self._handle_disconnect,
                                 daemon=True).start()

    # ------------------------------------------------------------------
    # Receiving (background thread)
    # ------------------------------------------------------------------

    def _start_recv_loop(self):
        self._recv_buffer = ""
        self._recv_thread = threading.Thread(target=self._recv_loop,
                                             name="NuiSyncRecv")
        self._recv_thread.daemon = True
        self._recv_thread.start()

    def _recv_loop(self):
        """Read from socket, dispatch messages. Ping/pong handled here."""
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
                while "\n" in self._recv_buffer:
                    line, self._recv_buffer = \
                        self._recv_buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except (ValueError, KeyError):
                        xbmc.log("[NuiSync] Bad JSON: %s" % line[:100],
                                 xbmc.LOGWARNING)
                        continue

                    cmd = msg.get("cmd", "")

                    # Handle ping/pong at network layer
                    if cmd == "ping":
                        self.send({"cmd": "pong"})
                        continue
                    elif cmd == "pong":
                        self._last_pong_time = time.time()
                        continue

                    # Forward everything else to the player layer
                    self._on_message(msg)

            except socket.timeout:
                continue
            except OSError:
                self._handle_disconnect()
                return

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

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Tear everything down cleanly."""
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

        self._on_status("Disconnected")
