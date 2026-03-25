"""
player.py — xbmc.Player subclass for NuiSync~ (host-as-authority model)

Host is the single source of truth:
    - Host sends play/pause/resume/seek/stop commands + periodic sync
    - Host NEVER adjusts its own playback based on the client
    - Client receives commands and applies them locally

Hybrid pause + seek sync (smooth and comfy~):
    - Drift < tolerance (2s):  do nothing
    - Client BEHIND by 2-15s:  seek forward to host position
    - Client AHEAD by 2-5s:    micro-pause (briefly pause, let host
                                catch up, auto-resume)
    - Client AHEAD by 5-15s:   seek to host position
    - Drift > hard_seek_threshold (15s): emergency hard seek

Drift smoothing prevents over-correction from network jitter:
    require 2+ consistent readings before correcting, plus a
    cooldown between corrections.

Fen Light / Cocoscrapers / TorBox integration:
    Works automatically -- Kodi fires the same Player callbacks
    regardless of which addon initiated playback. onAVStarted fires
    after the TorBox stream URL is resolved and buffering completes.
"""

import time
import threading

import xbmc
import xbmcgui

# How often the host sends its position to the client
SYNC_INTERVAL = 2.0

# Max seconds to correct via micro-pause (beyond this, seek instead)
MICRO_PAUSE_MAX = 5.0

# Estimated one-way network delay for Hamachi
LATENCY_COMPENSATION = 0.5

# Number of drift readings required before correcting
DRIFT_HISTORY_SIZE = 3

# Minimum seconds between corrections
CORRECTION_COOLDOWN = 4.0


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
            speed_max:            (unused, kept for service.py compat)
            speed_min:            (unused, kept for service.py compat)
        """
        super(NuiSyncPlayer, self).__init__()
        self._net = network
        self._is_host = is_host
        self._tolerance = desync_tolerance
        self._hard_seek_threshold = hard_seek_threshold

        # Thread-safe suppression to prevent echo loops.
        # When we apply a remote command, we set a timestamp; any local
        # callback that fires before that timestamp is suppressed.
        self._suppress_lock = threading.Lock()
        self._suppress_until = 0.0

        # Buffering sync — pause everyone when someone buffers
        self._is_buffering = False
        self._peer_buffering = False
        self._paused_for_buffering = False  # we paused playback due to peer buffering
        self._buffering_check_thread = None
        self._buffering_check_running = False

        # Sync heartbeat thread (host only)
        self._sync_thread = None
        self._sync_running = False

        # Drift smoothing state (client only)
        self._drift_history = []
        self._last_correction_time = 0.0
        self._pause_correction_active = False

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
        self._start_buffering_check()
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

        # -- Both sides handle buffering and reactions --
        if cmd == "buffering":
            self._handle_peer_buffering(msg.get("state", False))
            return
        if cmd == "reaction":
            emoji = msg.get("emoji", "")
            if emoji:
                xbmcgui.Dialog().notification(
                    "NuiSync", emoji, xbmcgui.NOTIFICATION_INFO, 3000)
            return

        # -- Host: respond to state_request, ignore everything else --
        if self._is_host:
            if cmd == "state_request":
                self._send_state_response()
            return

        # -- Client: apply commands from the host --
        self._suppress_for(0.5)

        if cmd == "play":
            url = msg.get("url", "")
            t = msg.get("time", 0.0)
            xbmc.log("[NuiSync] Remote play: %s @ %.1f" % (url, t),
                     xbmc.LOGINFO)
            self._drift_history.clear()
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

        elif cmd == "pause":
            t = msg.get("time", 0.0)
            xbmc.log("[NuiSync] Remote pause @ %.1f" % t, xbmc.LOGINFO)
            self._drift_history.clear()
            if self.isPlaying():
                self._ensure_paused()
                self.seekTime(t)

        elif cmd == "resume":
            t = msg.get("time", 0.0)
            xbmc.log("[NuiSync] Remote resume @ %.1f" % t, xbmc.LOGINFO)
            self._drift_history.clear()
            if self.isPlaying():
                self._ensure_playing()
                self.seekTime(t)

        elif cmd == "seek":
            t = msg.get("time", 0.0)
            xbmc.log("[NuiSync] Remote seek -> %.1f" % t, xbmc.LOGINFO)
            self._drift_history.clear()
            if self.isPlaying():
                self.seekTime(t)

        elif cmd == "stop":
            xbmc.log("[NuiSync] Remote stop", xbmc.LOGINFO)
            self._drift_history.clear()
            self.stop()

        elif cmd == "sync":
            self._handle_sync(msg.get("time", 0.0))

        elif cmd == "state_response":
            self._apply_state_response(msg)

        elif cmd == "buffering":
            self._peer_buffering = msg.get("state", False)

    # ==================================================================
    #  Hybrid pause + seek sync (CLIENT ONLY -- smooth and comfy~)
    # ==================================================================

    def _handle_sync(self, host_time):
        """Correct client drift using pause or seek.

        Uses drift smoothing to avoid over-correction from network
        jitter: requires multiple consistent readings before acting.

            - Within tolerance:       do nothing
            - Behind by 2-15s:        seek forward
            - Ahead by 2-5s:          micro-pause to let host catch up
            - Ahead by 5-15s:         seek to host position
            - Beyond threshold (15s): emergency hard seek
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

        # Compensate for network latency (host is likely further along
        # than the timestamp says by the time we receive it)
        adjusted_host_time = host_time + LATENCY_COMPENSATION

        # drift > 0 means client is BEHIND the host
        drift = adjusted_host_time - local_time
        abs_drift = abs(drift)
        now = time.time()

        # Emergency: huge drift, skip smoothing and seek immediately
        if abs_drift > self._hard_seek_threshold:
            xbmc.log("[NuiSync] Emergency seek: drift %.1fs -> %.1f" %
                     (drift, adjusted_host_time), xbmc.LOGINFO)
            self._seek_correction(adjusted_host_time)
            return

        # Record drift history for smoothing
        self._drift_history.append((now, drift))
        if len(self._drift_history) > DRIFT_HISTORY_SIZE:
            self._drift_history = self._drift_history[-DRIFT_HISTORY_SIZE:]

        # Need enough readings before correcting
        if len(self._drift_history) < 2:
            return

        # Respect cooldown between corrections
        if now - self._last_correction_time < CORRECTION_COOLDOWN:
            return

        # Check consistency: all readings must be in the same direction
        # Mixed signs = network jitter, not real drift
        signs = [d > 0 for _, d in self._drift_history]
        if not (all(signs) or not any(signs)):
            return

        avg_drift = sum(d for _, d in self._drift_history) / len(
            self._drift_history)
        abs_avg = abs(avg_drift)

        if abs_avg <= self._tolerance:
            return  # within tolerance, nothing to do~

        if avg_drift > 0:
            # Client is BEHIND host -> seek forward
            xbmc.log("[NuiSync] Behind by %.1fs, seeking to %.1f" %
                     (abs_avg, adjusted_host_time), xbmc.LOGINFO)
            self._seek_correction(adjusted_host_time)
        else:
            # Client is AHEAD of host
            if abs_avg <= MICRO_PAUSE_MAX:
                # Small lead: micro-pause to let host catch up
                xbmc.log("[NuiSync] Ahead by %.1fs, micro-pausing~" %
                         abs_avg, xbmc.LOGINFO)
                self._micro_pause_correction(abs_avg)
            else:
                # Large lead: seek is less disruptive than a long pause
                xbmc.log("[NuiSync] Ahead by %.1fs, seeking to %.1f" %
                         (abs_avg, adjusted_host_time), xbmc.LOGINFO)
                self._seek_correction(adjusted_host_time)

    def _seek_correction(self, target_time):
        """Seek to target position and reset drift tracking."""
        self._suppress_for(1.0)
        self.seekTime(target_time)
        self._drift_history.clear()
        self._last_correction_time = time.time()

    def _micro_pause_correction(self, pause_duration):
        """Briefly pause to let the host catch up, then auto-resume.

        Runs in a background thread so the message handler isn't blocked.
        """
        if self._pause_correction_active:
            return  # don't stack corrections
        self._pause_correction_active = True

        def _do_pause():
            try:
                self._suppress_for(pause_duration + 1.0)
                self._ensure_paused()

                # Wait in small increments so we can bail if playback stops
                waited = 0.0
                while waited < pause_duration:
                    xbmc.sleep(200)
                    waited += 0.2
                    if not self.isPlaying():
                        break

                self._ensure_playing()
                self._drift_history.clear()
                self._last_correction_time = time.time()
            finally:
                self._pause_correction_active = False

        t = threading.Thread(target=_do_pause, name="NuiSyncMicroPause")
        t.daemon = True
        t.start()

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
        self._drift_history.clear()

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

    # ==================================================================
    #  Buffering sync — pause for peer, resume when both ready
    # ==================================================================

    def _start_buffering_check(self):
        """Start a thread that monitors Player.Caching and tells the peer."""
        if self._buffering_check_running:
            return
        self._buffering_check_running = True
        self._buffering_check_thread = threading.Thread(
            target=self._buffering_check_loop, name="NuiSyncBuffCheck")
        self._buffering_check_thread.start()

    def _buffering_check_loop(self):
        monitor = xbmc.Monitor()
        while (self._buffering_check_running
               and self._net.connected
               and not monitor.abortRequested()):
            is_buf = xbmc.getCondVisibility("Player.Caching")
            if is_buf != self._is_buffering:
                self._is_buffering = is_buf
                self._net.send({"cmd": "buffering", "state": is_buf})
                if is_buf:
                    xbmc.log("[NuiSync] I'm buffering", xbmc.LOGINFO)
                else:
                    xbmc.log("[NuiSync] Done buffering", xbmc.LOGINFO)
            if not self.isPlaying():
                break
            if monitor.waitForAbort(0.5):
                break
        self._buffering_check_running = False

    def _handle_peer_buffering(self, is_buffering):
        """Peer started or stopped buffering — pause/resume accordingly."""
        self._peer_buffering = is_buffering
        if not self.isPlaying():
            return

        if is_buffering and not self._paused_for_buffering:
            # Peer is buffering — pause so they don't fall behind
            paused = xbmc.getCondVisibility("Player.Paused")
            if not paused:
                xbmc.log("[NuiSync] Pausing for peer buffering", xbmc.LOGINFO)
                self._suppress_for(1.0)
                self._paused_for_buffering = True
                self.pause()
        elif not is_buffering and self._paused_for_buffering:
            # Peer done buffering — resume
            paused = xbmc.getCondVisibility("Player.Paused")
            if paused:
                xbmc.log("[NuiSync] Resuming after peer buffering",
                         xbmc.LOGINFO)
                self._suppress_for(1.0)
                self.pause()  # toggles back to play
            self._paused_for_buffering = False

    # ==================================================================
    #  Reactions
    # ==================================================================

    def send_reaction(self, emoji):
        """Send a reaction emoji to the peer."""
        self._net.send({"cmd": "reaction", "emoji": emoji})
        # Show locally too
        xbmcgui.Dialog().notification("NuiSync", emoji,
                                      xbmcgui.NOTIFICATION_INFO, 3000)

    def cleanup(self):
        """Stop sync, clean up drift state."""
        self._stop_sync()
        self._buffering_check_running = False
        self._drift_history.clear()
