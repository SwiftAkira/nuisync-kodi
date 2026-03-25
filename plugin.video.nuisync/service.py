"""
service.py — Background service for NuiSync~

Runs for the entire Kodi session. Polls window properties set by
default.py to know when the user wants to host/join/disconnect.

Manages:
    - NuiSyncNetwork (socket connection + reconnection + NAT traversal)
    - NuiSyncPlayer  (playback event hooks, host-authority model)
    - On-screen status overlay (subtitle-area transparent WindowDialog)

Connection modes:
    - "host"      → UPnP auto-forward + session code + TCP listen
    - "join_code" → decode session code → TCP connect
    - "join"      → direct IP connect (legacy Hamachi / LAN fallback)

Toggle overlay via the addon menu or with:
    RunScript(plugin.video.nuisync,toggle_overlay)
"""

import threading

import xbmc
import xbmcgui
import xbmcaddon

from network import NuiSyncNetwork, STATE_CONNECTED, STATE_RECONNECTING, \
    STATE_DISCONNECTED
from player import NuiSyncPlayer

ADDON = xbmcaddon.Addon("plugin.video.nuisync")

# Overlay auto-hide delay in seconds
OVERLAY_AUTO_HIDE = 5


# ======================================================================
#  On-screen status overlay — subtitle-area transparent window
# ======================================================================

class StatusOverlay(xbmcgui.WindowDialog):
    """Transparent floating overlay in the subtitle area~

    Uses xbmcgui.WindowDialog -- borderless, transparent, floats above
    all Kodi screens including fullscreen video.

    Position: bottom-center, roughly where subtitles appear on 1080p.
    """

    LABEL_WIDTH = 600
    LABEL_HEIGHT = 40
    LABEL_X = (1920 - 600) // 2   # 660, centered
    LABEL_Y = 920                  # subtitle zone

    def __init__(self):
        super(StatusOverlay, self).__init__()

        # Semi-transparent dark background for readability
        self._bg = xbmcgui.ControlImage(
            x=self.LABEL_X - 10,
            y=self.LABEL_Y - 5,
            width=self.LABEL_WIDTH + 20,
            height=self.LABEL_HEIGHT + 10,
            filename="",
            colorDiffuse="99000000",
        )

        self._label = xbmcgui.ControlLabel(
            x=self.LABEL_X,
            y=self.LABEL_Y,
            width=self.LABEL_WIDTH,
            height=self.LABEL_HEIGHT,
            label="",
            font="font13",
            textColor="0xFFFF99CC",   # soft pink~
            alignment=0x00000002 | 0x00000004,
        )

        self.addControls([self._bg, self._label])

        self._visible = False
        self._last_text = ""
        self._auto_hide_timer = None
        self._lock = threading.Lock()

    def update(self, text, auto_hide=True):
        """Update text and show the overlay~"""
        with self._lock:
            self._last_text = text
            self._label.setLabel("NuiSync: %s" % text)

            if not self._visible:
                self.show()
                self._visible = True

            if self._auto_hide_timer:
                self._auto_hide_timer.cancel()
                self._auto_hide_timer = None

            if auto_hide and OVERLAY_AUTO_HIDE > 0:
                self._auto_hide_timer = threading.Timer(
                    OVERLAY_AUTO_HIDE, self._do_hide)
                self._auto_hide_timer.daemon = True
                self._auto_hide_timer.start()

    def toggle(self):
        """Toggle visibility."""
        with self._lock:
            if self._visible:
                self._do_hide()
            elif self._last_text:
                self._label.setLabel("NuiSync: %s" % self._last_text)
                self.show()
                self._visible = True

    def _do_hide(self):
        try:
            self.close()
        except RuntimeError:
            pass
        self._visible = False

    def dismiss(self):
        with self._lock:
            if self._auto_hide_timer:
                self._auto_hide_timer.cancel()
                self._auto_hide_timer = None
            self._do_hide()
            self._last_text = ""

    @property
    def last_text(self):
        return self._last_text


# ======================================================================
#  Settings helper
# ======================================================================

def _get_setting(key, default, cast=float):
    """Read an addon setting with a fallback default."""
    val = ADDON.getSetting(key)
    if not val:
        return default
    try:
        return cast(val)
    except (ValueError, TypeError):
        return default


# ======================================================================
#  Main service loop
# ======================================================================

def run_service():
    monitor = xbmc.Monitor()
    win = xbmcgui.Window(10000)
    overlay = StatusOverlay()

    network = None
    player = None
    session_active = False

    def on_message(msg):
        """Forward non-transport messages to the player."""
        if player:
            player.handle_remote(msg)

        # Show peer buffering state on overlay
        if msg.get("cmd") == "buffering":
            if msg.get("state"):
                overlay.update("Friend is buffering~", auto_hide=False)
            else:
                overlay.update("In sync~", auto_hide=True)

    def on_status(text):
        xbmc.log("[NuiSync] Status: %s" % text, xbmc.LOGINFO)
        auto_hide = "Reconnecting" not in text and "Code:" not in text
        overlay.update(text, auto_hide=auto_hide)

    def _make_network():
        """Create a NuiSyncNetwork with current settings."""
        auto_recon = ADDON.getSetting("auto_reconnect") != "false"
        recon_attempts = int(_get_setting("reconnect_attempts", 5, int))
        recon_delay = int(_get_setting("reconnect_delay", 3, int))
        return NuiSyncNetwork(
            on_message, on_status,
            auto_reconnect=auto_recon,
            reconnect_attempts=recon_attempts,
            reconnect_delay=recon_delay,
        )

    def _make_player(net, is_host):
        """Create a NuiSyncPlayer with current settings."""
        tolerance = _get_setting("sync_tolerance", 2.0)
        hard_seek = _get_setting("hard_seek_threshold", 15.0)
        speed_max = _get_setting("speed_max", 1.5)
        speed_min = _get_setting("speed_min", 0.8)
        return NuiSyncPlayer(
            net, is_host=is_host,
            desync_tolerance=tolerance,
            hard_seek_threshold=hard_seek,
            speed_max=speed_max, speed_min=speed_min,
        )

    def start_host():
        nonlocal network, player, session_active
        port = int(_get_setting("port", 9876, int))
        use_upnp = ADDON.getSetting("use_upnp") != "false"

        network = _make_network()
        player = _make_player(network, is_host=True)

        def _host():
            nonlocal session_active
            success = network.host(port, use_upnp=use_upnp)
            if success:
                session_active = True
                win.setProperty("nuisync.active", "true")
                # Show session code in a dialog for easy sharing
                code = network.session_code
                if code:
                    xbmcgui.Dialog().ok(
                        "NuiSync",
                        "Friend connected!\n"
                        "Session code was: %s" % code)
            else:
                on_status("Host cancelled")
                cleanup()

        t = threading.Thread(target=_host, name="NuiSyncHost")
        t.daemon = True
        t.start()

    def start_join_code():
        """Join via session code (NAT-traversal friendly)."""
        nonlocal network, player, session_active
        code = win.getProperty("nuisync.session_code")

        network = _make_network()
        player = _make_player(network, is_host=False)

        on_status("Connecting via code~")
        success = network.join_by_code(code)
        if success:
            session_active = True
            win.setProperty("nuisync.active", "true")
        else:
            xbmcgui.Dialog().ok(
                "NuiSync",
                "Couldn't connect with code %s~\n\n"
                "Make sure your friend is hosting and the code is "
                "correct. If it still fails, try 'Join with Direct IP' "
                "with Hamachi or a VPN." % code)
            cleanup()

    def start_join():
        """Join via direct IP (legacy / Hamachi fallback)."""
        nonlocal network, player, session_active
        ip = win.getProperty("nuisync.host_ip")
        port = int(_get_setting("port", 9876, int))

        network = _make_network()
        player = _make_player(network, is_host=False)

        success = network.join(ip, port)
        if success:
            session_active = True
            win.setProperty("nuisync.active", "true")
        else:
            xbmcgui.Dialog().ok("NuiSync",
                                "Couldn't connect to %s:%d~" % (ip, port))
            cleanup()

    def cleanup():
        nonlocal network, player, session_active
        if player:
            player.cleanup()
            player = None
        if network:
            network.shutdown()
            network = None
        session_active = False
        win.setProperty("nuisync.active", "")
        win.clearProperty("nuisync.session_code")
        overlay.dismiss()

    # ----- Main loop -----
    xbmc.log("[NuiSync] Service started~", xbmc.LOGINFO)

    while not monitor.abortRequested():
        # Check for role commands from the plugin UI
        role = win.getProperty("nuisync.role")
        if role:
            win.clearProperty("nuisync.role")

            if role == "host":
                cleanup()
                start_host()
            elif role == "join_code":
                cleanup()
                start_join_code()
            elif role == "join":
                cleanup()
                start_join()
            elif role == "disconnect":
                cleanup()
                overlay.update("See you next time~", auto_hide=True)

        # Check for overlay toggle
        toggle = win.getProperty("nuisync.toggle_overlay")
        if toggle:
            win.clearProperty("nuisync.toggle_overlay")
            overlay.toggle()

        # Monitor connection state
        if session_active and network:
            state = network.state
            if state == STATE_DISCONNECTED:
                xbmcgui.Dialog().notification(
                    "NuiSync", "Friend disconnected~",
                    xbmcgui.NOTIFICATION_WARNING, 3000)
                cleanup()

        if monitor.waitForAbort(0.5):
            break

    cleanup()
    xbmc.log("[NuiSync] Service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    run_service()