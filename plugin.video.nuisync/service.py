"""
service.py — Background service for NuiSync~

Runs for the entire Kodi session. Polls window properties set by
default.py to know when the user wants to host/join/disconnect.

Manages:
    - NuiSyncNetwork (WebSocket relay or direct TCP)
    - NuiSyncPlayer  (playback event hooks, host-authority model)

Connection modes:
    - "host"      → Create room on relay, wait for friend
    - "join"      → Join room on relay via code
    - "join_direct" → Direct TCP (LAN / Hamachi fallback)
"""

import xbmc
import xbmcgui
import xbmcaddon

from network import NuiSyncNetwork, STATE_DISCONNECTED
from player import NuiSyncPlayer

ADDON = xbmcaddon.Addon("plugin.video.nuisync")


def _get_setting(key, default, cast=float):
    val = ADDON.getSetting(key)
    if not val:
        return default
    try:
        return cast(val)
    except (ValueError, TypeError):
        return default


def run_service():
    monitor = xbmc.Monitor()
    win = xbmcgui.Window(10000)

    network = None
    player = None
    session_active = False

    def _notify(text, time_ms=4000, icon=xbmcgui.NOTIFICATION_INFO):
        try:
            xbmcgui.Dialog().notification("NuiSync", text, icon, time_ms)
        except RuntimeError:
            pass

    def on_message(msg):
        if player:
            player.handle_remote(msg)

    def on_status(text):
        xbmc.log("[NuiSync] Status: %s" % text, xbmc.LOGINFO)
        _notify(text)

    def _make_network():
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

    def start_host(room_code):
        nonlocal network, player, session_active
        network = _make_network()
        player = _make_player(network, is_host=True)

        def _host():
            nonlocal session_active
            success = network.host(room_code=room_code)
            if success:
                session_active = True
                win.setProperty("nuisync.active", "true")
                on_status("Friend connected!")
            else:
                cleanup()

        import threading
        t = threading.Thread(target=_host, name="NuiSyncHost")
        t.start()

    def start_join(code):
        nonlocal network, player, session_active
        network = _make_network()
        player = _make_player(network, is_host=False)

        on_status("Connecting~")
        success = network.join(code)
        if success:
            session_active = True
            win.setProperty("nuisync.active", "true")
        else:
            xbmcgui.Dialog().ok(
                "NuiSync",
                "Couldn't connect with code %s~\n\n"
                "Make sure your friend is hosting and the code "
                "is correct." % code)
            cleanup()

    def start_join_direct(ip):
        nonlocal network, player, session_active
        port = int(_get_setting("port", 9876, int))
        network = _make_network()
        player = _make_player(network, is_host=False)

        success = network.join_direct(ip, port)
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
        win.clearProperty("nuisync.room_code")

    # ----- Main loop -----
    xbmc.log("[NuiSync] Service started~", xbmc.LOGINFO)

    try:
        while not monitor.abortRequested():
            role = win.getProperty("nuisync.role")
            if role:
                win.clearProperty("nuisync.role")

                # Read properties BEFORE cleanup (cleanup clears them)
                room_code = win.getProperty("nuisync.room_code")
                host_ip = win.getProperty("nuisync.host_ip")

                if role == "host":
                    cleanup()
                    start_host(room_code)
                elif role == "join":
                    cleanup()
                    start_join(room_code)
                elif role == "join_direct":
                    cleanup()
                    start_join_direct(host_ip)
                elif role == "disconnect":
                    cleanup()
                    _notify("See you next time~")

            # Check for reaction
            if session_active and player:
                reaction = win.getProperty("nuisync.reaction")
                if reaction:
                    win.clearProperty("nuisync.reaction")
                    player.send_reaction(reaction)

            if session_active and network:
                if network.state == STATE_DISCONNECTED:
                    _notify("Friend disconnected~",
                            icon=xbmcgui.NOTIFICATION_WARNING)
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