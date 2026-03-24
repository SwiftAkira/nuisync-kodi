"""
default.py — Entry point for plugin.video.nuisync (NuiSync~)

Shows a cute menu: Host / Join / Toggle Overlay / Disconnect.
Stores the chosen action into a window property so the background
service (service.py) can pick it up.

Also handles the toggle_overlay argument so it can be bound to a
keyboard/remote shortcut via Kodi's keymap system or Favourites:
    RunScript(plugin.video.nuisync,toggle_overlay)
"""

import sys

import xbmc
import xbmcgui
import xbmcaddon

ADDON = xbmcaddon.Addon("plugin.video.nuisync")
ADDON_NAME = ADDON.getAddonInfo("name")


def main():
    win = xbmcgui.Window(10000)

    # ---- Handle RunScript argument (for keymap / favourites) ----
    if len(sys.argv) > 1 and sys.argv[1] == "toggle_overlay":
        win.setProperty("nuisync.toggle_overlay", "true")
        return

    is_active = win.getProperty("nuisync.active") == "true"

    # ---- Active session menu ----
    if is_active:
        options = [
            "Show / Hide Overlay~",
            "Disconnect",
        ]
        choice = xbmcgui.Dialog().select(ADDON_NAME, options)
        if choice == 0:
            win.setProperty("nuisync.toggle_overlay", "true")
        elif choice == 1:
            win.setProperty("nuisync.role", "disconnect")
        return

    # ---- New session menu ----
    options = [
        "Host a NuiSync session~",
        "Join a NuiSync session~",
    ]
    choice = xbmcgui.Dialog().select(ADDON_NAME, options)

    if choice == 0:
        port = ADDON.getSetting("port") or "9876"
        xbmcgui.Dialog().ok(
            ADDON_NAME,
            "Hosting on port %s~\n"
            "Share your Hamachi IP (25.x.x.x) with your friend!\n\n"
            "Waiting for them to connect..." % port,
        )
        win.setProperty("nuisync.role", "host")

    elif choice == 1:
        saved_ip = ADDON.getSetting("hamachi_ip") or "25.0.0.1"
        ip = xbmcgui.Dialog().input(
            "Enter Host's Hamachi IP~",
            defaultt=saved_ip,
            type=xbmcgui.INPUT_IPADDRESS,
        )
        if not ip:
            return
        ADDON.setSetting("hamachi_ip", ip)
        win.setProperty("nuisync.role", "join")
        win.setProperty("nuisync.host_ip", ip)


if __name__ == "__main__":
    main()
