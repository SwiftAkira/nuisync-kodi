"""
player.py — xbmc.Player subclass for NuiSync~ (host-as-authority model)

Host is the single source of truth:
    - Host sends play/pause/resume/seek/stop commands + periodic sync
    - Host NEVER adjusts its own playback based on the client
    - Client receives commands and applies them locally
    - Client adjusts playback speed to gradually catch up or slow
      down, avoiding jarring hard seeks

Speed-based sync (smooth and comfy~):
    - Drift < tolerance (2s):  do nothing, play at 1.0x
    - Drift between tolerance and hard_seek_threshold:
        Behind -> speed up (1.2x / 1.5x)
        Ahead  -> slow down (0.8x)
    - Drift > hard_seek_threshold (15s): hard seek as last resort

Fen Light / Cocoscrapers / TorBox integration:
    Works automatically -- Kodi fires the same Player callbacks
    regardless of which addon initiated playback. onAVStarted fires
    after the TorBox stream URL is resolved and buffering completes.
"""

import time
import json
import threading

import xbmc
import xbmcgui

# How often the host sends its position to the client
SYNC_INTERVAL = 2.0

# Kodi supports these tempo speeds (pitch-corrected audio)
# We pick from this set for smooth speed adjustment
TEMPO_SPEEDS = [0.8, 1.0, 1.2, 1.5]


class NuiSyncPlayer(xbmc.Player):
    """Intercepts local playback events. Host broadcasts, client obeys~"""

    def __init__(self, network, is_host, desync_tolerance=2.0,
                 hard_seek_threshold=15.0, speed_max=1.5, speed_min=0.8):
        """
        Args:
            network:              NuiSyncNetwork instance.
            is_host:              True if this side is the authority.
            desync_tolerance:     Seconds of drift before any correction.
            hard_seek_threshold:  Seconds of drift before hard seeking.
            speed_max:            Max playback speed for catching up.
            speed_min:            Min playback speed for slowing down.
        """
        super(NuiSyncPlayer, self).__init__()
        self._net = network
        self._is_host = is_host
        self._tolerance = desync_tolerance
        self._hard_seek_threshold = hard_seek_threshold
        self._speed_max = speed_max
        self._speed_min = speed_min

        # Current adjusted speed (1.0 = normal)
        self._current_speed = 1.0

        # Thread-safe suppression to prevent echo loops.
        # When we apply a remote command, we set a timestamp; any local
        # callback that fires before that timestamp is suppressed.
        self._suppress_lock = threading.Lock()
        self._suppress_until = 0.0

        # Buffering awareness
        self._is_buffering = False
        self._peer_buffering = False

        # Sync heartbeat thread (host only)
        self._sync_thread = None
        self._sync_running = False

    # ==================================================================
    #  Suppression helpers
    # ==================================================================

    def _suppress_for(self, seconds=0.5):
        """Mark that local callbacks should be suppressed for a duration."""
        with self._suppress_lock:
            self._suppress_until = time.time() + seconds

    def _is_suppressed(self):
        """Check if we're currently suppressing local callbacks."""
        with self._suppress_lock:
            return time.time() < self._suppress_until

    # ==================================================================
    #  Local -> Remote  (Kodi callbacks -- HOST ONLY sends commands)
    # ==================================================================

    def onAVStarted(self):
        """Fires after stream is resolved and buffering is done.
        Reliable for Fen Light + TorBox resolved streams~"""
        if self._is_suppressed() or not self._is_host:
            return
        url = self._current_url()
        t = self._current_time()
        xbmc.log("[NuiSync] Host play: %s @ %.1f" % (url, t),
                 xbmc.LOGINFO)
        self._net.send({"cmd": "play", "url": url, "time": t})
        self._start_sync()

    def onPlayBackPaused(self):
        if self._is_suppressed() or not self._is_host:
            return
        t = self._current_time()
        xbmc.log("[NuiSync] Host pause @ %.1f" % t, xbmc.LOGINFO)
        self._net.send({"cmd": "pause", "time": t})

    def onPlayBackResumed(self):
        if self._is_suppressed() or not self._is_host:
            return
        t = self._current_time()
        xbmc.log("[NuiSync] Host resume @ %.1f" % t, xbmc.LOGINFO)
        self._net.send({"cmd": "resume", "time": t})

    def onPlayBackSeek(self, seek_time, seek_offset):
        if self._is_suppressed() or not self._is_host:
            return
        t = seek_time / 1000.0  # ms -> seconds
        xbmc.log("[NuiSync] Host seek -> %.1f" % t, xbmc.LOGINFO)
        self._net.send({"cmd": "seek", "time": t})

    def onPlayBackStopped(self):
        if self._is_suppressed() or not self._is_host:
            return
        xbmc.log("[NuiSync] Host stop", xbmc.LOGINFO)
        self._net.send({"cmd": "stop"})
        self._stop_sync()

    def onPlayBackEnded(self):
        self.onPlayBackStopped()

    # ==================================================================
    #  Remote -> Local  (called by service.py when a message arrives)
    # ==================================================================

    def handle_remote(self, msg):
        """Apply a remote command locally.

        Host ignores playback commands from the client.
        Client applies playback commands from the host~
        """
        cmd = msg.get("cmd", "")

        # -- Host: respond to state_request, ignore everything else --
        if self._is_host:
            if cmd == "state_request":
                self._send_state_response()
            elif cmd == "buffering":
                self._peer_buffering = msg.get("state", False)
            return

        # -- Client: apply commands from the host --
        self._suppress_for(0.5)

        if cmd == "play":
            url = msg.get("url", "")
            t = msg.get("time", 0.0)
            xbmc.log("[NuiSync] Remote play: %s @ %.1f" % (url, t),
                     xbmc.LOGINFO)
            if self.isPlaying() and self._current_url() == url:
                self.seekTime(t)
            else:
                try:
                    self.play(url)
                except Exception as exc:
                    xbmc.log("[NuiSync] Failed to play URL: %s" % exc,
                             xbmc.LOGERROR)
                    xbmcgui.Dialog().notification(
                        "NuiSync",
                        "Couldn't play what the host started~",
                        xbmcgui.NOTIFICATION_WARNING, 4000)
                    return
                self._deferred_seek(t)
            self._reset_speed()

        elif cmd == "pause":
            t = msg.get("time", 0.0)
            xbmc.log("[NuiSync] Remote pause @ %.1f" % t, xbmc.LOGINFO)
            if self.isPlaying():
                self._ensure_paused()
                self.seekTime(t)
            self._reset_speed()

        elif cmd == "resume":
            t = msg.get("time", 0.0)
            xbmc.log("[NuiSync] Remote resume @ %.1f" % t, xbmc.LOGINFO)
            if self.isPlaying():
                self._ensure_playing()
                self.seekTime(t)
            self._reset_speed()

        elif cmd == "seek":
            t = msg.get("time", 0.0)
            xbmc.log("[NuiSync] Remote seek -> %.1f" % t, xbmc.LOGINFO)
            if self.isPlaying():
                self.seekTime(t)
            self._reset_speed()

        elif cmd == "stop":
            xbmc.log("[NuiSync] Remote stop", xbmc.LOGINFO)
            self._reset_speed()
            self.stop()

        elif cmd == "sync":
            self._handle_sync(msg.get("time", 0.0))

        elif cmd == "state_response":
            self._apply_state_response(msg)

        elif cmd == "buffering":
            self._peer_buffering = msg.get("state", False)

    # ==================================================================
    #  Speed-based sync (CLIENT ONLY -- smooth and comfy~)
    # ==================================================================

    def _handle_sync(self, host_time):
        """Gradually adjust client speed to match the host position.

        Instead of jarring hard seeks, we speed up or slow down:
            - Within tolerance:  1.0x (do nothing)
            - Behind by 2-15s:   1.2x or 1.5x
            - Ahead:             0.8x
            - Beyond threshold:  hard seek as last resort
        """
        if self._is_host or not self.isPlaying():
            return

        # Don't adjust while buffering
        if xbmc.getCondVisibility("Player.Caching"):
            return

        try:
            local_time = self.getTime()
            total_time = self.getTotalTime()
        except RuntimeError:
            return

        # Don't seek past the end of the video
        if total_time > 0 and host_time > total_time:
            return

        # drift > 0 means client is BEHIND the host
        drift = host_time - local_time
        abs_drift = abs(drift)

        if abs_drift <= self._tolerance:
            # Within tolerance -- play at normal speed
            if self._current_speed != 1.0:
                xbmc.log("[NuiSync] Drift %.1fs within tolerance, "
                         "restoring 1.0x~" % drift, xbmc.LOGINFO)
                self._set_playback_speed(1.0)
            return

        if abs_drift > self._hard_seek_threshold:
            # Way too far off -- hard seek as a last resort
            xbmc.log("[NuiSync] Drift %.1fs exceeds threshold, "
                     "hard seeking to %.1f" % (drift, host_time),
                     xbmc.LOGINFO)
            self._suppress_for(0.5)
            self.seekTime(host_time)
            self._set_playback_speed(1.0)
            return

        # Between tolerance and threshold -- speed correction
        if drift > 0:
            # Client is behind -- speed up~
            target_speed = self._pick_catchup_speed(abs_drift)
            if target_speed != self._current_speed:
                xbmc.log("[NuiSync] Behind by %.1fs -> %.1fx" %
                         (abs_drift, target_speed), xbmc.LOGINFO)
                self._set_playback_speed(target_speed)
        else:
            # Client is ahead -- slow down~
            target_speed = self._speed_min
            target_speed = self._nearest_tempo(target_speed)
            if target_speed != self._current_speed:
                xbmc.log("[NuiSync] Ahead by %.1fs -> %.1fx" %
                         (abs_drift, target_speed), xbmc.LOGINFO)
                self._set_playback_speed(target_speed)

    def _pick_catchup_speed(self, abs_drift):
        """Choose a catch-up speed based on how far behind we are.

        Uses a tiered approach with Kodi's supported tempo speeds:
            tolerance -> mid-range:   1.2x
            mid-range -> threshold:   speed_max (1.5x)
        """
        midpoint = (self._tolerance + self._hard_seek_threshold) / 2.0

        if abs_drift < midpoint:
            target = 1.2
        else:
            target = self._speed_max

        return self._nearest_tempo(target)

    @staticmethod
    def _nearest_tempo(target):
        """Snap to the nearest Kodi-supported tempo speed."""
        return min(TEMPO_SPEEDS, key=lambda s: abs(s - target))

    def _set_playback_speed(self, speed):
        """Set playback speed using Kodi JSON-RPC tempo mode.

        Tempo mode preserves audio pitch unlike regular FF/RW.
        Kodi accepts decimal speed values via JSON-RPC and will
        snap to the nearest supported tempo preset internally.
        """
        if speed == self._current_speed:
            return
        request = json.dumps({
            "jsonrpc": "2.0",
            "method": "Player.SetSpeed",
            "params": {"playerid": 1, "speed": speed},
            "id": 1
        })
        try:
            response = xbmc.executeJSONRPC(request)
            result = json.loads(response)
            if "error" in result:
                xbmc.log("[NuiSync] SetSpeed error: %s" %
                         result["error"], xbmc.LOGWARNING)
                return
            # Read back actual speed Kodi applied
            actual = result.get("result", {}).get("speed", speed)
            self._current_speed = actual
        except Exception as exc:
            xbmc.log("[NuiSync] SetSpeed failed: %s" % exc,
                     xbmc.LOGWARNING)

    def _reset_speed(self):
        """Reset playback speed to normal."""
        if self._current_speed != 1.0:
            self._set_playback_speed(1.0)

    # ==================================================================
    #  Sync heartbeat (HOST ONLY -- sends position to client)
    # ==================================================================

    def _start_sync(self):
        if self._sync_running:
            return
        if not self._is_host:
            return  # Client doesn't send sync heartbeats
        self._sync_running = True
        self._sync_thread = threading.Thread(target=self._sync_loop,
                                             name="NuiSyncHeartbeat")
        self._sync_thread.daemon = True
        self._sync_thread.start()

    def _stop_sync(self):
        self._sync_running = False

    def _sync_loop(self):
        """Host periodically sends its playback position to the client~"""
        monitor = xbmc.Monitor()
        while (self._sync_running
               and self._net.connected
               and not monitor.abortRequested()):
            if self.isPlaying():
                # Skip sending sync if peer is buffering
                if not self._peer_buffering:
                    try:
                        t = self.getTime()
                        self._net.send({"cmd": "sync", "time": t})
                    except RuntimeError:
                        pass
            if monitor.waitForAbort(SYNC_INTERVAL):
                break

    # ==================================================================
    #  State request/response (for reconnection sync-up)
    # ==================================================================

    def _send_state_response(self):
        """Host sends its full playback state so a reconnected client
        can sync up immediately~"""
        playing = self.isPlaying()
        paused = xbmc.getCondVisibility("Player.Paused")
        url = self._current_url()
        t = self._current_time()
        self._net.send({
            "cmd": "state_response",
            "playing": playing,
            "paused": paused,
            "url": url,
            "time": t,
        })
        xbmc.log("[NuiSync] Sent state response: playing=%s paused=%s "
                 "time=%.1f" % (playing, paused, t), xbmc.LOGINFO)

    def _apply_state_response(self, msg):
        """Client applies the host's full state after reconnecting."""
        url = msg.get("url", "")
        t = msg.get("time", 0.0)
        playing = msg.get("playing", False)
        paused = msg.get("paused", False)

        if not playing:
            return

        xbmc.log("[NuiSync] Applying host state: url=%s time=%.1f "
                 "paused=%s" % (url[:60], t, paused), xbmc.LOGINFO)

        self._suppress_for(1.0)

        if not self.isPlaying() or self._current_url() != url:
            try:
                self.play(url)
            except Exception as exc:
                xbmc.log("[NuiSync] state_response play failed: %s" %
                         exc, xbmc.LOGERROR)
                return
            self._deferred_seek(t)
        else:
            self.seekTime(t)

        if paused:
            xbmc.sleep(500)
            self._ensure_paused()

        self._reset_speed()

    # ==================================================================
    #  Helpers
    # ==================================================================

    def _current_url(self):
        try:
            return self.getPlayingFile()
        except RuntimeError:
            return ""

    def _current_time(self):
        try:
            return self.getTime()
        except RuntimeError:
            return 0.0

    def _deferred_seek(self, target_time, retries=20):
        """Wait for playback to start, then seek. Handles TorBox buffering~"""
        if target_time < 1.0:
            return

        def _do():
            for _ in range(retries):
                xbmc.sleep(500)
                if self.isPlaying():
                    self._suppress_for(0.5)
                    self.seekTime(target_time)
                    return

        t = threading.Thread(target=_do, name="NuiSyncSeek")
        t.daemon = True
        t.start()

    def _ensure_paused(self):
        """Make sure playback is actually paused."""
        xbmc.sleep(100)
        if xbmc.getCondVisibility("Player.Playing"):
            self.pause()

    def _ensure_playing(self):
        """Make sure playback is actually playing (not paused)."""
        xbmc.sleep(100)
        if xbmc.getCondVisibility("Player.Paused"):
            self.pause()

    def cleanup(self):
        """Stop sync, reset speed."""
        self._stop_sync()
        self._reset_speed()
