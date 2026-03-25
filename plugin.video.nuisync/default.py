"""
default.py — Entry point for plugin.video.nuisync (NuiSync~)

Menu:
    - Host a session (generates room code, copies to clipboard)
    - Join with Code
    - Join with Direct IP (LAN/Hamachi fallback)
    - Disconnect (when active)
"""

import sys

import xbmc
import xbmcgui
import xbmcaddon

from network import generate_room_code

ADDON = xbmcaddon.Addon("plugin.video.nuisync")
ADDON_NAME = ADDON.getAddonInfo("name")


def main():
    win = xbmcgui.Window(10000)
    is_active = win.getProperty("nuisync.active") == "true"

    if is_active:
        choice = xbmcgui.Dialog().select(ADDON_NAME, ["Disconnect"])
        if choice == 0:
            win.setProperty("nuisync.role", "disconnect")
        return

    options = [
        "Host a NuiSync session~",
        "Join with Session Code~",
        "Join with Direct IP (LAN/Hamachi)",
    ]
    choice = xbmcgui.Dialog().select(ADDON_NAME, options)

    if choice == 0:
        _do_host()
    elif choice == 1:
        _do_join()
    elif choice == 2:
        _do_join_direct()


def _do_host():
    win = xbmcgui.Window(10000)
    code = generate_room_code()
    formatted = "%s-%s" % (code[:3], code[3:]) if len(code) > 3 else code

    # Copy to clipboard
    try:
        import subprocess
        proc = subprocess.Popen(
            ["clip.exe"] if sys.platform == "win32"
            else ["xclip", "-selection", "clipboard"],
            stdin=subprocess.PIPE)
        proc.communicate(code.encode("utf-8"))
    except Exception:
        pass

    xbmcgui.Dialog().ok(
        ADDON_NAME,
        "Your Session Code (copied to clipboard):\n\n"
        "[B]%s[/B]\n\n"
        "Share this code with your friend!\n"
        "They select 'Join with Session Code' and enter it.\n"
        "Waiting for them after you press OK~" % formatted)

    win.setProperty("nuisync.room_code", code)
    win.setProperty("nuisync.role", "host")


def _do_join():
    win = xbmcgui.Window(10000)
    code = xbmcgui.Dialog().input(
        "Enter Session Code~",
        type=xbmcgui.INPUT_ALPHANUM,
    )
    if not code:
        return
    code = code.strip().upper().replace(" ", "").replace("-", "")
    if len(code) < 3 or len(code) > 10:
        xbmcgui.Dialog().ok(ADDON_NAME, "Invalid code~")
        return
    win.setProperty("nuisync.room_code", code)
    win.setProperty("nuisync.role", "join")


def _do_join_direct():
    win = xbmcgui.Window(10000)
    saved_ip = ADDON.getSetting("hamachi_ip") or "25.0.0.1"
    ip = xbmcgui.Dialog().input(
        "Enter Host's IP~",
        defaultt=saved_ip,
        type=xbmcgui.INPUT_IPADDRESS,
    )
    if not ip:
        return
    ADDON.setSetting("hamachi_ip", ip)
    win.setProperty("nuisync.host_ip", ip)
    win.setProperty("nuisync.role", "join_direct")


if __name__ == "__main__":
    main()