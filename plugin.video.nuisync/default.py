"""
default.py — Entry point for plugin.video.nuisync (NuiSync~)

Shows a cute menu:
    - Host (auto-connect via session code)
    - Join via Session Code
    - Join via Direct IP (legacy / Hamachi fallback)
    - Toggle Overlay / Disconnect (when active)

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

from nathelper import discover_public_address, try_upnp_forward, encode_session

ADDON = xbmcaddon.Addon("plugin.video.nuisync")
ADDON_NAME = ADDON.getAddonInfo("name")


def _get_setting(key, default, cast=float):
    val = ADDON.getSetting(key)
    if not val:
        return default
    try:
        return cast(val)
    except (ValueError, TypeError):
        return default


def main():
    win = xbmcgui.Window(10000)

    is_active = win.getProperty("nuisync.active") == "true"

    # ---- Active session menu ----
    if is_active:
        options = [
            "Disconnect",
        ]
        choice = xbmcgui.Dialog().select(ADDON_NAME, options)
        if choice == 0:
            win.setProperty("nuisync.role", "disconnect")
        return

    # ---- New session menu ----
    options = [
        "Host a NuiSync session~",
        "Join with Session Code~",
        "Join with Direct IP (Hamachi/LAN)",
    ]
    choice = xbmcgui.Dialog().select(ADDON_NAME, options)

    if choice == 0:
        _do_host()
    elif choice == 1:
        _do_join_code()
    elif choice == 2:
        _do_join_direct()


def _do_host():
    """Generate session code up-front and show it before waiting."""
    win = xbmcgui.Window(10000)
    port = int(_get_setting("port", 9876, int))
    use_upnp = ADDON.getSetting("use_upnp") != "false"
    dialog = xbmcgui.Dialog()

    # Show a progress dialog while setting up
    pbar = xbmcgui.DialogProgress()
    pbar.create(ADDON_NAME, "Setting up connection~")
    pbar.update(10, "Discovering public address~")

    session_code = None
    upnp_ok = False

    # Try UPnP
    if use_upnp and not pbar.iscanceled():
        pbar.update(30, "Trying UPnP auto-port-forward~")
        mapping = try_upnp_forward(port, protocol="TCP")
        if mapping:
            upnp_ok = True
            xbmc.log("[NuiSync] UPnP succeeded in default.py", xbmc.LOGINFO)
            # Get public IP from UPnP or STUN
            pbar.update(50, "Getting public address~")
            pub_ip = mapping.get_external_ip()
            if not pub_ip:
                result = discover_public_address()
                pub_ip = result[0] if result else None
            if pub_ip:
                session_code = encode_session(pub_ip, port)
            # Clean up — service.py will create its own mapping
            mapping.teardown()
        else:
            xbmc.log("[NuiSync] UPnP not available", xbmc.LOGINFO)

    # If UPnP failed, still try STUN for the code
    if not session_code and not pbar.iscanceled():
        pbar.update(60, "Checking public address via STUN~")
        result = discover_public_address()
        if result:
            session_code = encode_session(result[0], port)

    pbar.close()

    if pbar.iscanceled():
        return

    # Show the session code (or instructions) in a dialog
    if session_code:
        # Copy to clipboard
        import subprocess
        try:
            proc = subprocess.Popen(
                ["clip.exe"] if sys.platform == "win32" else ["xclip", "-selection", "clipboard"],
                stdin=subprocess.PIPE)
            proc.communicate(session_code.encode("utf-8"))
        except Exception:
            pass  # clipboard not available, no big deal

        # Store the code so service.py can reuse it
        win.setProperty("nuisync.session_code_host", session_code)
        dialog.ok(
            ADDON_NAME,
            "Your Session Code (copied to clipboard):\n\n"
            "[B]%s[/B]\n\n"
            "Share this code with your friend!\n"
            "They select 'Join with Session Code' and enter it.\n"
            "%s"
            "Waiting for them after you press OK~" % (
                session_code,
                "(UPnP port forward active)\n" if upnp_ok
                else "(No UPnP — friend may need direct IP)\n",
            ))
    else:
        dialog.ok(
            ADDON_NAME,
            "Couldn't detect your public address~\n\n"
            "Hosting on port %d anyway.\n"
            "Share your IP with your friend manually,\n"
            "or they can use 'Join with Direct IP'.\n\n"
            "Waiting for them after you press OK~" % port)

    win.setProperty("nuisync.role", "host")


def _do_join_code():
    """Join via session code."""
    win = xbmcgui.Window(10000)
    code = xbmcgui.Dialog().input(
        "Enter Session Code~",
        type=xbmcgui.INPUT_ALPHANUM,
    )
    if not code:
        return
    # Clean up the code
    code = code.strip().upper().replace(" ", "").replace("-", "")
    if len(code) != 10:
        xbmcgui.Dialog().ok(
            ADDON_NAME,
            "Invalid code. Codes look like XXXXX-XXXXX (10 characters)~")
        return
    # Format nicely
    formatted = "%s-%s" % (code[:5], code[5:])
    win.setProperty("nuisync.role", "join_code")
    win.setProperty("nuisync.session_code", formatted)


def _do_join_direct():
    """Join via direct IP (legacy Hamachi / LAN / manual port forward)."""
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
    win.setProperty("nuisync.role", "join")
    win.setProperty("nuisync.host_ip", ip)


if __name__ == "__main__":
    main()