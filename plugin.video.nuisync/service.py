"""
service.py — Background service for NuiSync~

Runs for the entire Kodi session. Polls window properties set by
default.py to know when the user wants to host/join/disconnect.

Manages:
    - NuiSyncNetwork (socket connection + reconnection + NAT traversal)
    - NuiSyncPlayer  (playback event hooks, host-authority model)

Connection modes:
    - "host"      → UPnP auto-forward + session code + TCP listen
    - "join_code" → decode session code → TCP connect
    - "join"      → direct IP connect (legacy Hamachi / LAN fallback)

Uses Kodi's built-in notification system for status messages instead
of a persistent WindowDialog — avoids crash on addon uninstall when
Kodi's CPythonInvoker force-kills the interpreter while GUI objects
are still alive.
"""

import xbmc
import xbmcgui
import xbmcaddon

from network import NuiSyncNetwork, STATE_CONNECTED, STATE_RECONNECTING, \
    STATE_DISCONNECTED
from player import NuiSyncPlayer

ADDON = xbmcaddon.Addon("plugin.video.nuisync")


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

    network = None
    player = None
    session_active = False

    def _notify(text, time_ms=4000, icon=xbmcgui.NOTIFICATION_INFO):
        """Show a Kodi notification — no persistent GUI objects."""
        try:
            xbmcgui.Dialog().notification("NuiSync", text, icon, time_ms)
        except RuntimeError:
            pass

    def on_message(msg):
        """Forward non-transport messages to the player."""
        if player:
            player.handle_remote(msg)

        if msg.get("cmd") == "buffering":
            if msg.get("state"):
                _notify("Friend is buffering~")
            else:
                _notify("In sync~")

    def on_status(text):
        xbmc.log("[NuiSync] Status: %s" % text, xbmc.LOGINFO)
        _notify(text)

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

        win.clearProperty("nuisync.session_code_host")

        def _host():
            nonlocal session_active
            success = network.host(port, use_upnp=use_upnp)
            if success:
                session_active = True
                win.setProperty("nuisync.active", "true")
                on_status("Friend connected!")
            else:
                on_status("Host cancelled")
                cleanup()

        import threading
        t = threading.Thread(target=_host, name="NuiSyncHost")
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

    # ----- Main loop -----
    xbmc.log("[NuiSync] Service started~", xbmc.LOGINFO)

    try:
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
                    _notify("See you next time~")

            # Monitor connection state
            if session_active and network:
                state = network.state
                if state == STATE_DISCONNECTED:
                    _notify("Friend disconnected~", icon=xbmcgui.NOTIFICATION_WARNING)
                    cleanup()

            if monitor.waitForAbort(0.5):
                break
    except Exception:
        pass

    try:
        cleanup()
    except Exception:
        pass
    xbmc.log("[NuiSync] Service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    run_service()