import logging
import os
import sys
import requests
import urllib.parse

from threading import RLock, Lock, Event, Thread
from queue import Queue
from collections import OrderedDict

from . import conffile
from .utils import synchronous, Timer, get_resource, get_plex_url
from .conf import settings
from .menu import OSDMenu
from .media import MediaType, Media

log = logging.getLogger('player')
mpv_log = logging.getLogger('mpv')

python_mpv_available=True
is_using_ext_mpv=False
if not settings.mpv_ext:
    try:
        import mpv
        log.info("Using libmpv1 playback backend.")
    except OSError:
        log.warning("Could not find libmpv1.")
        python_mpv_available=False

if settings.mpv_ext or not python_mpv_available:
    import python_mpv_jsonipc as mpv
    log.info("Using external mpv playback backend.")
    is_using_ext_mpv=True

_mpv_errors = (BrokenPipeError,)
if hasattr(mpv, "ShutdownError"):
    _mpv_errors = (BrokenPipeError, mpv.ShutdownError)

APP_NAME = 'plex-mpv-shim'

SUBTITLE_POS = {
    "top": 0,
    "bottom": 100,
    "middle": 80,
}

mpv_log_levels = {
    "fatal": mpv_log.error,
    "error": mpv_log.error,
    "warn": mpv_log.warning,
    "info": mpv_log.info
}

def mpv_log_handler(level, prefix, text):
    if level in mpv_log_levels:
        mpv_log_levels[level]("{0}: {1}".format(prefix, text))
    else:
        mpv_log.debug("{0}: {1}".format(prefix, text))

win_utils = None
if sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
    try:
        from . import win_utils
    except ModuleNotFoundError:
        log.warning("win_utils is not available.")

# Q: What is with the put_task call?
# A: Some calls to python-mpv require event processing.
#    put_task is used to deal with the events originating from
#    the event thread, which would cause deadlock if they run there.

class PlayerManager(object):
    """
    Manages the relationship between a ``Player`` instance and a ``Media``
    item.  This is designed to be used as a singleton via the ``playerManager``
    instance in this module.  All communication between a caller and either the
    current ``player`` or ``media`` instance should be done through this class
    for thread safety reasons as all methods that access the ``player`` or
    ``media`` are thread safe.
    """
    def __init__(self):
        self._media_item = None
        self._lock = RLock()
        self._finished_lock = Lock()
        self.last_update = Timer()
        self.__part = 1
        self.timeline_trigger = None
        self.action_trigger = None
        self.external_subtitles = {}
        self.external_subtitles_rev = {}
        self.url = None
        self.evt_queue = Queue()
        self.is_in_intro = False
        self.is_in_credits = False
        self.intro_has_triggered = False
        self.credits_has_triggered = False
        self._mpv_alive = False

        # Native playlist population state
        # Maps mpv playlist position -> Video item for all pre-loaded episodes
        self._playlist_items = {}
        # Maps mpv playlist position -> series seq index
        self._playlist_series_map = {}
        # Maps mpv playlist position -> pre-computed display title string.
        # Built from allLeaves XML (has correct grandparentTitle/index) so we
        # avoid the identifier-mismatch bug in Video.get_proper_title().
        self._playlist_titles = {}
        # True once at least one future episode has been appended (keep_open
        # gets flipped to False at this point so mpv can advance naturally)
        self._playlist_has_items = False
        # True once ALL series episodes have been appended
        self._playlist_populated = False
        # Cancel token: set() to abort an in-progress _populate_playlist thread
        self._playlist_cancel = Event()
        # Next mpv playlist position to assign (0-based; all episodes including
        # the one currently playing are included in the M3U8 replace)
        self._next_playlist_pos = 0
        # Current mpv playlist position (updated by _on_playlist_advance)
        self._current_playlist_pos = 0
        # When a full-playlist replace is in progress, holds the target series
        # position so _on_playlist_advance_inner can suppress transient pos
        # events (mpv briefly fires pos=0 before we jump to start_seq).
        self._playlist_expected_pos = None
        self._playlist_start_seq = None
        # True when mpv fired eof-reached=True for the current episode (natural
        # end-of-file).  _on_playlist_advance_inner checks this before calling
        # set_played() so that manual skips/jumps do NOT mark episodes watched.
        self._ep_finished_naturally = False
        # Port of the local stream proxy used for M3U8 URLs (set after init)
        self._stream_proxy_port = None

        self._init_mpv()
        self._stream_proxy_port = _start_stream_proxy(self)

    def _init_mpv(self):
        mpv_options = OrderedDict()
        if is_using_ext_mpv:
            mpv_options.update(
                {
                    "start_mpv": settings.mpv_ext_start,
                    "ipc_socket": settings.mpv_ext_ipc,
                    "mpv_location": settings.mpv_ext_path,
                    "player-operation-mode": "cplayer",
                    "start_retries": settings.mpv_ext_start_retries,
                    "start_retry_delay_ms": settings.mpv_ext_start_retry_delay_ms,
                }
            )

        if settings.menu_mouse:
            if is_using_ext_mpv:
                mpv_options["script"] = get_resource("mouse.lua")
            else:
                mpv_options["scripts"] = get_resource("mouse.lua")

        conffile.get_dir(APP_NAME, "scripts")
        conffile.get_dir(APP_NAME, "fonts")
        conffile.get(APP_NAME, "input.conf", True)
        conffile.get(APP_NAME, "mpv.conf", True)

        if not (settings.mpv_ext and settings.mpv_ext_no_ovr):
            mpv_options["config"] = True
            mpv_options["config_dir"] = conffile.confdir(APP_NAME)

        self._player = mpv.MPV(input_default_bindings=True, input_vo_keyboard=True,
                               input_media_keys=settings.media_keys, log_handler=mpv_log_handler,
                               loglevel=settings.mpv_log_level, **mpv_options)
        self.menu = OSDMenu(self)
        if hasattr(self._player, 'osc'):
            self.enable_osc(settings.enable_osc)
        else:
            log.warning("This mpv version doesn't support on-screen controller.")

        if hasattr(self._player, "resume_playback"):
            self._player.resume_playback = False

        # Wrapper for on_key_press that ignores None.
        def keypress(key):
            def wrapper(func):
                if key is not None:
                    self._player.on_key_press(key)(func)
                return func
            return wrapper

        @self._player.on_key_press('CLOSE_WIN')
        @self._player.on_key_press('STOP')
        @keypress(settings.kb_stop)
        def handle_stop():
            log.warning("=== handle_stop TRIGGERED ===")
            # STOP / CLOSE_WIN means the user wants to stop playback.
            # Do NOT route through finished_callback (which auto-advances to
            # the next episode). Natural end-of-file is handled separately by
            # the eof-reached observer and the end-file event callback.
            self.put_task(self.stop_and_close)

        if not (settings.mpv_ext and settings.mpv_ext_no_keybinds):
            @keypress(settings.kb_prev)
            def handle_prev():
                self.put_task(self.play_prev)

            @keypress(settings.kb_next)
            def handle_next():
                self.put_task(self.play_next)

            @self._player.on_key_press('PREV')
            @self._player.on_key_press('XF86_PREV')
            def handle_media_prev():
                if settings.media_key_seek:
                    self._player.command("seek", -15)
                else:
                    self.put_task(self.play_prev)

            @self._player.on_key_press('NEXT')
            @self._player.on_key_press('XF86_NEXT')
            def handle_media_next():
                if settings.media_key_seek:
                    if self.is_in_intro:
                        self.skip_intro()
                    elif self.is_in_credits:
                        self.skip_credits()
                    else:
                        self._player.command("seek", 30)
                else:
                    self.put_task(self.play_next)

            @keypress(settings.kb_watched)
            def handle_watched():
                self.put_task(self.watched_skip)

            @keypress(settings.kb_unwatched)
            def handle_unwatched():
                self.put_task(self.unwatched_quit)

            @keypress(settings.kb_menu)
            def menu_open():
                if not self.menu.is_menu_shown:
                    self.menu.show_menu()
                else:
                    self.menu.hide_menu()

            @keypress(settings.kb_menu_esc)
            def menu_back():
                if self.menu.is_menu_shown:
                    self.menu.menu_action('back')
                else:
                    self._player.command('set', 'fullscreen', 'no')

            @keypress(settings.kb_menu_ok)
            def menu_ok():
                self.menu.menu_action('ok')

            @keypress(settings.kb_menu_left)
            def menu_left():
                if self.menu.is_menu_shown:
                    self.menu.menu_action('left')
                else:
                    self._player.command("seek", settings.seek_left)

            @keypress(settings.kb_menu_right)
            def menu_right():
                if self.menu.is_menu_shown:
                    self.menu.menu_action('right')
                else:
                    if self.is_in_intro:
                        self.skip_intro()
                    elif self.is_in_credits:
                        self.skip_credits()
                    else:
                        self._player.command("seek", settings.seek_right)

            @keypress(settings.kb_menu_up)
            def menu_up():
                if self.menu.is_menu_shown:
                    self.menu.menu_action('up')
                else:
                    if self.is_in_intro:
                        self.skip_intro()
                    elif self.is_in_credits:
                        self.skip_credits()
                    else:
                        self._player.command("seek", settings.seek_up)

            @keypress(settings.kb_menu_down)
            def menu_down():
                if self.menu.is_menu_shown:
                    self.menu.menu_action('down')
                else:
                    self._player.command("seek", settings.seek_down)

            @keypress(settings.kb_pause)
            def handle_pause():
                if self.menu.is_menu_shown:
                    self.menu.menu_action('ok')
                else:
                    self.toggle_pause()

            # This gives you an interactive python debugger prompt.
            @keypress(settings.kb_debug)
            def handle_debug():
                import pdb
                pdb.set_trace()

        # Fires when mpv moves to a different playlist entry (e.g. natural
        # end-of-file advance, or user jumping via playlist-next/prev).
        @self._player.property_observer('playlist-pos')
        def handle_playlist_pos(_name, pos):
            if pos is not None and pos >= 0:
                self.put_task(self._on_playlist_advance, pos)

        # Fires between episodes.
        @self._player.property_observer('eof-reached')
        def handle_end(_name, reached_end):
            log.warning("=== handle_end EOF OBSERVER TRIGGERED reached_end=%s media_item=%s ===" % (reached_end, self._media_item is not None))
            if self._media_item and reached_end:
                # Record that this episode ended naturally so _on_playlist_advance
                # knows it is safe to call set_played() for it.
                self._ep_finished_naturally = True
                has_lock = self._finished_lock.acquire(False)
                log.warning("=== handle_end: queuing finished_callback has_lock=%s ===" % has_lock)
                self.put_task(self.finished_callback, has_lock)

        # Fallback: mpv's end-file event fires reliably even when the
        # eof-reached property observer doesn't (e.g. with keep_open=yes,
        # loop-file=inf in user config, or certain mpv builds).
        @self._player.event_callback("end-file")
        def handle_end_file(event):
            try:
                if hasattr(event, "as_dict"):
                    event_dict = event.as_dict()
                else:
                    event_dict = event if isinstance(event, dict) else {}
                reason = event_dict.get("reason") if isinstance(event_dict, dict) else None
                if isinstance(reason, bytes):
                    reason = reason.decode("utf-8", errors="ignore")
                log.warning("=== end-file EVENT reason=%s media_item=%s ===" % (reason, self._media_item is not None))
                # reason "eof" = natural end of file. Other reasons (stop, quit,
                # redirect, error) should NOT trigger auto-advance.
                if self._media_item and reason == "eof":
                    has_lock = self._finished_lock.acquire(False)
                    log.warning("=== end-file: queuing finished_callback has_lock=%s ===" % has_lock)
                    self.put_task(self.finished_callback, has_lock)
            except Exception:
                log.warning("Error in end-file handler", exc_info=True)

        # Fires at the end.
        @self._player.property_observer("playback-abort")
        def handle_end_idle(_name, value):
            log.warning("=== handle_end_idle PLAYBACK-ABORT OBSERVER TRIGGERED value=%s ===" % value)
            if self._media_item and value and not self._media_item.parent.has_next:
                has_lock = self._finished_lock.acquire(False)
                log.warning("=== handle_end_idle: queuing finished_callback has_lock=%s ===" % has_lock)
                self.put_task(self.finished_callback, has_lock)

        @self._player.event_callback('client-message')
        def handle_client_message(event):
            try:
                # Python-MPV 1.0 uses a class/struct combination now
                if hasattr(event, "as_dict"):
                    event = event.as_dict()
                    if 'event' in event:
                        event['event'] = event['event'].decode('utf-8')
                    if 'args' in event:
                        event['args'] = [d.decode('utf-8') for d in event['args']]

                if "event_id" in event:
                    args = event["event"]["args"]
                else:
                    args = event["args"]
                if len(args) == 0:
                    return
                if args[0] == "shim-menu-select":
                    # Apparently this can happen...
                    if args[1] == "inf":
                        return
                    self.menu.mouse_select(int(args[1]))
                elif args[0] == "shim-menu-click":
                    self.menu.menu_action("ok")
            except Exception:
                log.warning("Error when processing client-message.", exc_info=True)

        @self._player.event_callback("shutdown")
        def handle_shutdown(event):
            log.info("mpv shutdown event received")
            if self._media_item:
                local_media_item = self._media_item
                self._media_item = None
                try:
                    if local_media_item.media_type == MediaType.VIDEO:
                        local_media_item.terminate_transcode()
                except Exception:
                    log.debug("Error terminating transcode after mpv shutdown.", exc_info=True)
            self.exec_stop_cmd()
            self._handle_mpv_disconnect()

        self._mpv_alive = True

    # Put a task to the event queue.
    # This ensures the task executes outside
    # of an event handler, which causes a crash.
    def put_task(self, func, *args):
        self.evt_queue.put([func, args])
        if self.action_trigger:
            self.action_trigger.set()

    # Trigger the timeline to update all
    # clients immediately.
    def timeline_handle(self):
        if self.timeline_trigger:
            self.timeline_trigger.set()

    def skip_marker(self, end_point):
        if not self._mpv_alive:
            return False
        if self._media_item.media_type == MediaType.VIDEO:
            try:
                self._player.playback_time = end_point
            except _mpv_errors:
                self._handle_mpv_disconnect()
                return False
            self.timeline_handle()
            return True
        return False

    def skip_intro(self):
        end_point = self._media_item.intro_end
        if self.skip_marker(end_point):
            self.is_in_intro = False

    def skip_credits(self):
        end_point = self._media_item.credits_end
        if self.skip_marker(end_point):
            self.is_in_credits = False

    def check_intro_or_credits(self):
        if ((settings.skip_intro_always or settings.skip_intro_prompt)
            and self._media_item is not None and self._media_item.media_type == MediaType.VIDEO and self._media_item.intro_start is not None
            and self._player.playback_time is not None
            and self._player.playback_time > self._media_item.intro_start
            and self._player.playback_time < self._media_item.intro_end):

            if not self.is_in_intro:
                if settings.skip_intro_always and not self.intro_has_triggered:
                    self.intro_has_triggered = True
                    self.skip_intro()
                    self._player.show_text("Skipped Intro", 3000, 1)
                elif settings.skip_intro_prompt:
                    self._player.show_text("Seek to Skip Intro", 3000, 1)
            self.is_in_intro = True
        else:
            self.is_in_intro = False

        # TODO de-duplicate this code in some way - it's ugly
        if ((settings.skip_credits_always or settings.skip_credits_prompt)
            and self._media_item is not None and self._media_item.media_type == MediaType.VIDEO and self._media_item.credits_start is not None
            and self._player.playback_time is not None
            and self._player.playback_time > self._media_item.credits_start
            and self._player.playback_time < self._media_item.credits_end):

            if not self.is_in_credits:
                if settings.skip_credits_always and not self.credits_has_triggered:
                    self.credits_has_triggered = True
                    self.skip_credits()
                    self._player.show_text("Skipped Credits", 3000, 1)
                elif settings.skip_credits_prompt:
                    self._player.show_text("Seek to Skip Credits", 3000, 1)
            self.is_in_credits = True
        else:
            self.is_in_credits = False


    @synchronous('_lock')
    def update(self):
        if not self._mpv_alive:
            return
        try:
            self.check_intro_or_credits()
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        while not self.evt_queue.empty():
            func, args = self.evt_queue.get()
            func(*args)
        try:
            if self._media_item and not self._player.playback_abort:
                if not self.is_paused():
                    self.last_update.restart()
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def _wait_for_playback_start(self, timeout=30.0):
        """
        Wait for mpv to either start playing (duration becomes non-None) or fail
        (playback-abort becomes True). Replaces wait_for_property("duration") with
        a version that cannot block forever when a stream returns a 404 or other
        error — in that case duration stays None forever and the old call would
        deadlock the _lock, preventing any subsequent play command from running.

        Returns True if playback started successfully, False on failure or timeout.
        """
        done = Event()
        started = [False]
        # observe_property fires the callback once immediately with the current
        # value, then again on every change. We skip that first "current value"
        # fire with a simple counter per observer.
        skip = [0, 0]  # [duration, abort]

        def on_duration(name, value):
            if skip[0] == 0:
                skip[0] = 1
                return
            if value is not None:
                started[0] = True
                done.set()

        def on_abort(name, value):
            if skip[1] == 0:
                skip[1] = 1
                return
            if value:
                done.set()  # started[0] stays False → caller knows it failed

        dur_id = self._player.bind_property_observer("duration", on_duration)
        abort_id = self._player.bind_property_observer("playback-abort", on_abort)

        timed_out = not done.wait(timeout=timeout)

        try:
            self._player.unbind_property_observer(dur_id)
            self._player.unbind_property_observer(abort_id)
        except Exception:
            pass

        if timed_out:
            log.warning("PlayerManager::_play_media timed out waiting for playback to start")

        return started[0]

    def play(self, media_item, offset=0):
        url = media_item.get_playback_url()
        if not url:
            log.error("PlayerManager::play no URL found")
            return

        self._play_media(media_item, url, offset)

    @synchronous('_lock')
    def _play_media(self, media_item, url, offset=0):
        if not self._mpv_alive:
            log.info("mpv is dead, reinitializing")
            self._init_mpv()

        self.url = url
        self.menu.hide_menu()

        if settings.log_decisions:
            log.debug("Playing: {0}".format(url))

        # Load the episode.  We try to pass force-media-title as a per-file
        # option so that entry 0 shows the episode name (not a raw URL) in
        # mpv's playlist selector.  Two fallbacks handle older mpv builds.
        try:
            _current_title = media_item.get_proper_title()
            _n = len(_current_title.encode('utf-8'))
            _safe = '%%%d%%%s' % (_n, _current_title)
            _opt  = 'force-media-title=' + _safe
            try:
                # mpv >= 0.35: loadfile url flags index options
                self._player.command("loadfile", self.url, "replace", 0, _opt)
            except Exception:
                try:
                    # mpv < 0.35: loadfile url flags options
                    self._player.command("loadfile", self.url, "replace", _opt)
                except Exception:
                    # last resort: plain play (no per-file title)
                    self._player.play(self.url)
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return

        if not self._wait_for_playback_start():
            # Stream failed (e.g. 404) or timed out. Returning here releases
            # _lock immediately so the next play command is never blocked.
            log.warning("PlayerManager::_play_media stream failed to start, aborting play")
            return

        # Remove any playlist entries left over from the previous episode/series.
        # playlist-clear deletes everything except the currently playing entry.
        try:
            self._player.command("playlist-clear")
        except Exception:
            pass

        try:
            if settings.fullscreen:
                self._player.fs = True
            _title0 = media_item.get_proper_title()
            self._player.force_media_title = _title0
            # Also set playlist/0/title so the playlist UI shows the correct
            # name for the currently-playing entry immediately.
            try:
                self._player["playlist/0/title"] = _title0
            except Exception:
                pass
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        self._media_item  = media_item
        self.__part = 1
        self.is_in_intro = False
        self.is_in_credits = False
        self.intro_has_triggered = False
        self.credits_has_triggered = False
        self.update_subtitle_visuals(False)
        self.upd_player_hide()
        self.external_subtitles = {}
        self.external_subtitles_rev = {}

        if win_utils and settings.raise_mpv:
            win_utils.raise_mpv()

        if offset > 0:
            try:
                self._player.playback_time = offset
            except _mpv_errors:
                self._handle_mpv_disconnect()
                return

        if media_item.media_type == MediaType.VIDEO and not media_item.is_transcode:
            try:
                audio_idx = media_item.get_audio_idx()
                if audio_idx is not None:
                    log.debug("PlayerManager::play selecting audio stream index=%s" % audio_idx)
                    self._player.audio = audio_idx

                sub_idx = media_item.get_subtitle_idx()
                xsub_id = media_item.get_external_sub_id()
                if sub_idx is not None:
                    log.debug("PlayerManager::play selecting subtitle index=%s" % sub_idx)
                    self._player.sub = sub_idx
                elif xsub_id is not None:
                    log.debug("PlayerManager::play selecting external subtitle id=%s" % xsub_id)
                    self.load_external_sub(xsub_id)
                else:
                    self._player.sub = 'no'
            except _mpv_errors:
                self._handle_mpv_disconnect()
                return

        try:
            self._player.pause = False
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        except Exception as _e:
            log.warning("_play_media: pause=False raised unexpected %s: %s — continuing" % (type(_e).__name__, _e))
        try:
            self.timeline_handle()
        except Exception as _e:
            log.warning("_play_media: timeline_handle raised %s: %s — continuing" % (type(_e).__name__, _e))
        try:
            if self._finished_lock.locked():
                self._finished_lock.release()
        except Exception as _e:
            log.warning("_play_media: _finished_lock.release raised %s: %s — continuing" % (type(_e).__name__, _e))

        # --- Native playlist population ---
        # Cancel any playlist-load from a previous episode and reset state.
        self._playlist_cancel.set()
        self._playlist_cancel = Event()
        self._playlist_items = {}
        self._playlist_series_map = {}
        self._playlist_titles = {}
        self._playlist_has_items = False
        self._playlist_populated = False
        self._playlist_expected_pos = None
        self._playlist_start_seq = None
        self._next_playlist_pos = 0
        self._current_playlist_pos = 0
        self._ep_finished_naturally = False

        # Kick off background load for TV episodes that have a loaded series.
        parent = media_item.parent if media_item else None
        log.info("_play_media: playlist check — media_item=%s type=%s _part_node=%s has_series=%s seq=%s series_len=%s" % (
            media_item is not None,
            getattr(media_item, 'media_type', None),
            getattr(media_item, '_part_node', 'MISSING') is not None,
            hasattr(parent, 'series'),
            getattr(parent, 'seq', 'N/A'),
            len(getattr(parent, 'series', [])) if hasattr(parent, 'series') else 'N/A',
        ))
        if (media_item is not None
                and media_item.media_type == MediaType.VIDEO
                and getattr(media_item, '_part_node', None) is not None
                and hasattr(parent, 'series')
                and parent.seq is not None
                and len(parent.series) > 1):
            cancel = self._playlist_cancel
            t = Thread(
                target=self._populate_playlist,
                args=(media_item, cancel),
                daemon=True,
                name="plex-playlist-loader"
            )
            t.start()
            log.info("_play_media: started playlist loader thread")

    def _populate_playlist(self, media_item, cancel):
        """
        Background thread: insert the full season into mpv's playlist without
        interrupting the currently playing episode.

        Strategy — append + playlist-move (no loadlist replace):
          Past episodes  (0..start_seq-1): appended as M3U8, then each entry is
            moved in front of the current episode one step at a time.  This
            shifts the current episode from pos 0 → start_seq without stopping
            it.
          Future episodes (start_seq+1..total-1): appended after the current
            episode in the normal way.

        The currently playing episode is never touched, so there is no restart
        or re-buffer.  After the moves, mpv playlist pos == series seq index
        for every entry.
        """
        import urllib.parse as _up
        import tempfile, os as _os

        parent = media_item.parent
        if not hasattr(parent, 'series') or parent.seq is None:
            log.debug("_populate_playlist: no series on parent, skipping")
            return

        start_seq = parent.seq
        total = len(parent.series)
        log.info("_populate_playlist: loading season (%d episodes, current seq %d/%d)" % (
            total, start_seq, total - 1))

        past_indices   = list(range(0, start_seq))
        future_indices = list(range(start_seq + 1, total))

        # ── Phase 1: collect episode data ────────────────────────────────────
        # No lock held, no mpv IPC — just HTTP fetches to Plex.
        past_episodes   = []  # [(series_seq, ep_video, url, title), ...]
        future_episodes = []

        for i in past_indices + future_indices:
            if cancel.is_set():
                log.debug("_populate_playlist: cancelled at seq %d (fetch phase)" % i)
                return

            video_node = parent.series[i]
            key = video_node.get('key')
            if not key:
                continue

            try:
                ep_media = Media(
                    parent.get_path(key),
                    parent.series, i,
                    parent.play_queue,
                    getattr(parent, 'play_queue_xml', None),
                    parent.media_type
                )
                ep_video = ep_media.get_media_item(0)
                if not ep_video or not ep_video._part_node:
                    log.warning("_populate_playlist: no video/part at seq %d" % i)
                    continue
                part_key = ep_video._part_node.get("key", "")
                url = get_plex_url(_up.urljoin(ep_media.server_url, part_key))
                if not url:
                    log.warning("_populate_playlist: empty URL at seq %d" % i)
                    continue
            except Exception as e:
                log.error("_populate_playlist: fetch error at seq %d: %s" % (i, e))
                continue

            # Build display title from XML node (has grandparentTitle etc.).
            title = None
            try:
                s_name  = video_node.get('grandparentTitle', '').strip()
                s_num   = int(video_node.get('parentIndex', 0))
                ep_num  = int(video_node.get('index', 0))
                ep_name = video_node.get('title', '').strip()
                if s_name and ep_num:
                    title = "%s - s%de%02d - %s" % (s_name, s_num, ep_num, ep_name)
            except Exception:
                pass
            if not title:
                try:
                    title = ep_video.get_proper_title()
                except Exception:
                    title = video_node.get('title', 'Episode %d' % i)

            log.info("_populate_playlist: seq %d title=%r" % (i, title))
            if i < start_seq:
                past_episodes.append((i, ep_video, url, title))
            else:
                future_episodes.append((i, ep_video, url, title))

        if cancel.is_set():
            log.debug("_populate_playlist: cancelled after fetch phase")
            return

        if not past_episodes and not future_episodes:
            log.debug("_populate_playlist: no episodes to add")
            return

        # ── Phase 2: update dicts, then append + move ────────────────────────
        # Populate dicts BEFORE any mpv IPC so the proxy and advance-handler
        # always find a valid Video object when an entry is first accessed.
        with self._lock:
            if cancel.is_set():
                return
            for i, ep_video, url, title in past_episodes + future_episodes:
                self._playlist_items[i] = ep_video
                self._playlist_series_map[i] = i
                self._playlist_titles[i] = title
            # Register the current episode so OSC navigation to it works.
            self._playlist_items[start_seq] = media_item
            self._playlist_series_map[start_seq] = start_seq

        if not self._mpv_alive:
            return

        # Suppress _on_playlist_advance events while the current episode is
        # being shifted from mpv pos 0 → start_seq by the playlist-move loop.
        self._playlist_expected_pos = start_seq

        # ── 2a: past episodes ─────────────────────────────────────────────────
        # After appending the past M3U8:
        #   mpv playlist: [current(0), past_ep0(1), past_ep1(2), ..., past_ep_{n-1}(n)]
        # After k-th move (playlist-move k+1 k):
        #   mpv shifts current one step right → eventually current is at start_seq.
        if past_episodes:
            past_tmp = None
            try:
                m3u8_lines = ["#EXTM3U"]
                for i, _ev, _url, title in past_episodes:
                    safe = title.replace("\\", "\\\\").replace(",", "\\,")
                    m3u8_lines.append("#EXTINF:-1,%s" % safe)
                    m3u8_lines.append("http://127.0.0.1:%d/stream/%d" % (
                        self._stream_proxy_port, i))
                m3u8_content = "\r\n".join(m3u8_lines) + "\r\n"
                fd, past_tmp = tempfile.mkstemp(suffix='.m3u8')
                with _os.fdopen(fd, 'w', encoding='utf-8') as fh:
                    fh.write(m3u8_content)
            except Exception as e:
                log.error("_populate_playlist: could not write past M3U8: %s" % e)
                past_tmp = None

            if past_tmp:
                try:
                    self._player.command("loadlist", past_tmp, "append")
                    log.info("_populate_playlist: appended %d past episodes" % len(past_episodes))
                except Exception as e:
                    log.error("_populate_playlist: loadlist append (past) failed: %s" % e)
                finally:
                    try:
                        _os.unlink(past_tmp)
                    except Exception:
                        pass

                if cancel.is_set():
                    return

                # Move each past entry in front of the current episode.
                # After move k: current is at pos k+1; next move targets k+2→k+1.
                for k in range(len(past_episodes)):
                    if cancel.is_set():
                        return
                    try:
                        self._player.command("playlist-move", k + 1, k)
                    except Exception as e:
                        log.error("_populate_playlist: playlist-move %d→%d failed: %s" % (
                            k + 1, k, e))
                        break

                log.info("_populate_playlist: %d moves done — current ep now at pos %d" % (
                    len(past_episodes), start_seq))

        # ── 2b: future episodes ───────────────────────────────────────────────
        if future_episodes:
            future_tmp = None
            try:
                m3u8_lines = ["#EXTM3U"]
                for i, _ev, _url, title in future_episodes:
                    safe = title.replace("\\", "\\\\").replace(",", "\\,")
                    m3u8_lines.append("#EXTINF:-1,%s" % safe)
                    m3u8_lines.append("http://127.0.0.1:%d/stream/%d" % (
                        self._stream_proxy_port, i))
                m3u8_content = "\r\n".join(m3u8_lines) + "\r\n"
                fd, future_tmp = tempfile.mkstemp(suffix='.m3u8')
                with _os.fdopen(fd, 'w', encoding='utf-8') as fh:
                    fh.write(m3u8_content)
            except Exception as e:
                log.error("_populate_playlist: could not write future M3U8: %s" % e)
                future_tmp = None

            if future_tmp:
                try:
                    self._player.command("loadlist", future_tmp, "append")
                    log.info("_populate_playlist: appended %d future episodes" % len(future_episodes))
                except Exception as e:
                    log.error("_populate_playlist: loadlist append (future) failed: %s" % e)
                finally:
                    try:
                        _os.unlink(future_tmp)
                    except Exception:
                        pass

        if cancel.is_set():
            return

        # Disable keep_open so mpv auto-advances naturally to the next episode.
        try:
            self._player.keep_open = False
            log.info("_populate_playlist: keep_open disabled — mpv manages playlist")
        except Exception:
            pass

        self._playlist_has_items = True
        self._playlist_populated = True
        log.info("_populate_playlist: done — %d past + 1 current + %d future episodes" % (
            len(past_episodes), len(future_episodes)))

    def _on_playlist_advance(self, pos):
        """
        Called (via put_task) whenever mpv's playlist-pos property changes.
        Updates _media_item to the new episode and resets playback-state flags
        so Plex timeline reporting stays accurate.
        """
        try:
            self._on_playlist_advance_inner(pos)
        except Exception:
            log.error("_on_playlist_advance: unhandled exception at pos %d" % pos, exc_info=True)

    def _on_playlist_advance_inner(self, pos):
        # Always track where mpv is, even if we don't have metadata yet.
        self._current_playlist_pos = pos

        # During a loadlist replace + playlist-pos repositioning, mpv briefly
        # fires pos=0 (and any other positions below start_seq) before we
        # jump to the current episode.  Suppress those transient events so we
        # don't wrongly mark the current episode as played or swap _media_item.
        if self._playlist_expected_pos is not None:
            if pos < self._playlist_expected_pos:
                log.debug("_on_playlist_advance: suppressing transient pos %d "
                          "(expected %d during replace)" % (pos, self._playlist_expected_pos))
                return
            else:
                # Reached or passed the target position — clear the guard.
                self._playlist_expected_pos = None

        if pos not in self._playlist_items:
            log.debug("_on_playlist_advance: pos %d not in playlist_items (items=%s)" % (
                pos, sorted(self._playlist_items.keys())))
            return

        log.info("_on_playlist_advance: mpv moved to playlist pos %d" % pos)

        ep_video = self._playlist_items[pos]

        # Mark the previous episode as played only when it ended naturally
        # (eof-reached=True fired).  Manual skips, jumps, and prev/next
        # hotkeys do NOT set _ep_finished_naturally, so they correctly leave
        # the episode marked as unwatched.
        if self._media_item and ep_video is not self._media_item:
            if self._ep_finished_naturally:
                try:
                    self._media_item.set_played()
                except Exception:
                    log.debug("_on_playlist_advance: set_played failed", exc_info=True)
            else:
                log.debug("_on_playlist_advance: skipping set_played — episode was skipped, not finished")
        self._ep_finished_naturally = False  # reset for the incoming episode
        ep_media = ep_video.parent

        self._media_item = ep_video
        self.__part = 1
        self.is_in_intro = False
        self.is_in_credits = False
        self.intro_has_triggered = False
        self.credits_has_triggered = False
        self.external_subtitles = {}
        self.external_subtitles_rev = {}

        try:
            title = self._playlist_titles.get(pos) or ep_video.get_proper_title()
            self._player.force_media_title = title
        except Exception:
            pass

        self.update_subtitle_visuals(False)
        self.upd_player_hide()

        # Select correct audio / subtitle tracks for direct-play content.
        # NOTE: We intentionally do NOT call _handle_mpv_disconnect() here.
        # When the user jumps non-sequentially in the playlist, mpv may fire
        # rapid end-file/playlist-pos events for transient positions.  An IPC
        # error on those transient entries is normal; killing _mpv_alive on a
        # transient error is what causes the crash.  Just log and continue —
        # the next genuine mpv error will be caught by the main loop.
        if ep_media.media_type == MediaType.VIDEO and not ep_video.is_transcode:
            try:
                audio_idx = ep_video.get_audio_idx()
                if audio_idx is not None:
                    self._player.audio = audio_idx

                sub_idx = ep_video.get_subtitle_idx()
                xsub_id = ep_video.get_external_sub_id()
                if sub_idx is not None:
                    self._player.sub = sub_idx
                elif xsub_id is not None:
                    self.load_external_sub(xsub_id)
                else:
                    self._player.sub = 'no'
            except Exception:
                log.debug("_on_playlist_advance: track selection failed (transient IPC error during rapid playlist nav is OK)", exc_info=True)

        self.timeline_handle()

        # Release the finished-lock so future callbacks aren't blocked.
        if self._finished_lock.locked():
            self._finished_lock.release()

    def exec_stop_cmd(self):
        if settings.stop_cmd:
            os.system(settings.stop_cmd)

    @synchronous('_lock')
    def stop(self, playend=False):
        if not self._media_item or not self._mpv_alive:
            self.exec_stop_cmd()
            return

        try:
            if not playend and self._player.playback_abort:
                self.exec_stop_cmd()
                return
        except _mpv_errors:
            self._handle_mpv_disconnect()
            self.exec_stop_cmd()
            return

        if not playend:
            log.debug("PlayerManager::stop stopping playback of %s" % self._media_item)

        local_media_item = self._media_item
        if local_media_item.media_type == MediaType.VIDEO:
            local_media_item.terminate_transcode()

        self._media_item  = None
        try:
            self._player.command("stop")
            self._player.pause = False
        except _mpv_errors:
            self._handle_mpv_disconnect()
        self.timeline_handle()

        if not playend:
            self.exec_stop_cmd()

    def stop_and_close(self):
        log.info("stop_and_close: stopping playback")
        self.stop()
        if not self._mpv_alive:
            return
        try:
            self._player.keep_open = False
            self._player.force_window = False
            self._player.command("stop")
        except _mpv_errors:
            self._handle_mpv_disconnect()
        log.info("stop_and_close: done")

    @synchronous('_lock')
    def get_volume(self, percent=False):
        if self._player and self._mpv_alive:
            try:
                if not percent:
                    return self._player.volume / 100
                return self._player.volume
            except _mpv_errors:
                self._handle_mpv_disconnect()
        return 0 if percent else 0

    @synchronous('_lock')
    def toggle_pause(self):
        if not self._mpv_alive:
            return
        try:
            if not self._player.playback_abort:
                self._player.pause = not self._player.pause
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        self.timeline_handle()

    @synchronous('_lock')
    def seek(self, offset):
        """
        Seek to ``offset`` seconds
        """
        if not self._mpv_alive:
            return
        try:
            if not self._player.playback_abort:
                if self.is_in_intro and offset > self._player.playback_time:
                    self.skip_intro()
                elif self.is_in_credits and offset > self._player.playback_time:
                    self.skip_credits()
                else:
                    self._player.playback_time = offset
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        self.timeline_handle()

    @synchronous('_lock')
    def set_volume(self, pct):
        if not self._mpv_alive:
            return
        try:
            if not self._player.playback_abort:
                self._player.volume = pct
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        self.timeline_handle()

    @synchronous('_lock')
    def get_state(self):
        if not self._mpv_alive:
            return "stopped"
        try:
            if self._player.playback_abort:
                return "stopped"

            if self._player.pause:
                return "paused"
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return "stopped"

        return "playing"
    
    @synchronous('_lock')
    def is_paused(self):
        if not self._mpv_alive:
            return False
        try:
            if not self._player.playback_abort:
                return self._player.pause
        except _mpv_errors:
            self._handle_mpv_disconnect()
        return False

    @synchronous('_lock')
    def finished_callback(self, has_lock):
        log.warning("=== finished_callback CALLED has_lock=%s _playlist_has_items=%s ===" % (has_lock, self._playlist_has_items))
        if not self._media_item:
            log.warning("=== finished_callback: no _media_item ===")
            return

        # ── Managed-playlist guard ──────────────────────────────────────────
        # This check must be FIRST — before is_multipart(), before any
        # play_next / play() logic.  When mpv owns the playlist (M3U8 loaded
        # via loadlist), it handles every transition by itself, including
        # non-sequential OSC jumps.  Calling play_next() here issues
        # playlist-next force and causes the "jump to last episode" bug:
        #   • eof-reached fires True + end-file both queue finished_callback
        #   • the second call (has_lock=False) bypassed the old check and
        #     fell into is_multipart() → play_next → playlist-next → skip
        #   • race condition: _playlist_has_items was False while Phase-4
        #     waited on _lock (held by this very callback), so the old else-
        #     branch guard also failed.
        # The only safe action is to release the finished-lock and return.
        if self._playlist_has_items:
            log.info("finished_callback: managed playlist active — deferring all transitions to mpv")
            if self._finished_lock.locked():
                self._finished_lock.release()
            return
        # ────────────────────────────────────────────────────────────────────

        self._media_item.set_played()

        if self._media_item.is_multipart():
            if has_lock:
                log.debug("PlayerManager::finished_callback media is multi-part, checking for next part")
                # Try to select the next part
                next_part = self.__part+1
                if self._media_item.select_part(next_part):
                    self.__part = next_part
                    log.debug("PlayerManager::finished_callback starting next part")
                    self.play(self._media_item)
            else:
                log.debug("PlayerManager::finished_callback No lock for multi-part, queuing next part check...")
                # Queue play_next which will acquire lock and handle multipart or next episode
                self.put_task(self.play_next)

        else:
            # Fallback (no managed playlist): check if the next position is
            # already in our dict.
            next_pos = self._current_playlist_pos + 1
            if next_pos in self._playlist_items:
                log.info("finished_callback: pos %d in playlist, deferring to mpv" % next_pos)
                if self._finished_lock.locked():
                    self._finished_lock.release()
                return

            next_media = self._media_item.parent.get_next() if settings.auto_play else None
            log.warning("=== finished_callback: auto_play=%s, next_media=%s ===" % (settings.auto_play, next_media is not None))
            if next_media and has_lock:
                log.warning("=== finished_callback: PLAYING NEXT (has_lock=True) ===")
                self.play(next_media.get_media_item(0))
            elif next_media:
                log.debug("PlayerManager::finished_callback No lock, queuing retry...")
                # Retry after lock becomes available
                import threading
                timer = threading.Timer(
                    0.5,
                    lambda: self.put_task(self.play_next)
                )
                timer.daemon = True
                timer.start()
            else:
                log.warning("=== finished_callback: NO NEXT MEDIA, STOPPING ===")
                if settings.media_ended_cmd:
                    os.system(settings.media_ended_cmd)
                # Stop playback since there's no next episode
                self.put_task(self.stop_and_close)
                log.debug("PlayerManager::finished_callback reached end")
                self.stop(playend=True)

    @synchronous('_lock')
    def watched_skip(self):
        if not self._media_item:
            return

        self._media_item.set_played()
        self.play_next()

    @synchronous('_lock')
    def unwatched_quit(self):
        if not self._media_item:
            return

        self._media_item.set_played(False)
        self.stop()

    @synchronous('_lock')
    def play_next(self):
        log.warning("=== play_next CALLED ===")
        if not self._media_item:
            log.warning("=== play_next: no media_item ===")
            return False

        # When mpv owns the playlist, delegate to its built-in command so the
        # native playlist position stays in sync.
        if self._playlist_has_items and self._mpv_alive:
            try:
                self._player.command("playlist-next", "force")
                log.info("play_next: issued playlist-next command")
                return True
            except _mpv_errors:
                self._handle_mpv_disconnect()
                return False

        try:
            log.warning("=== play_next: calling get_next() ===")
            next_media = self._media_item.parent.get_next()
            log.warning("=== play_next: get_next() returned %s ===" % (next_media is not None))
            if next_media:
                log.warning("=== play_next: got next media, playing... ===")
                try:
                    self.play(next_media.get_media_item(0))
                    log.warning("=== play_next: successfully played ===")
                    return True
                except Exception as e:
                    log.error("PlayerManager::play_next exception while playing: %s" % str(e))
                    return False
            else:
                log.warning("=== play_next: no next media available ===")
                return False
        except Exception as e:
            log.error("=== play_next: EXCEPTION: %s ===" % str(e))
            return False

    @synchronous('_lock')
    def skip_to(self, key):
        media = self._media_item.parent.get_from_key(key)
        if media:
            self.play(media.get_media_item(0))
            return True
        return False

    @synchronous('_lock')
    def play_prev(self):
        if self._playlist_has_items and self._mpv_alive:
            try:
                self._player.command("playlist-prev", "force")
                log.info("play_prev: issued playlist-prev command")
                return True
            except _mpv_errors:
                self._handle_mpv_disconnect()
                return False

        prev_media = self._media_item.parent.get_prev() if self._media_item else None
        if prev_media:
            self.play(prev_media.get_media_item(0))
            return True
        return False

    @synchronous('_lock')
    def restart_playback(self):
        if not self._mpv_alive:
            return False
        current_time = self._player.playback_time
        self.play(self._media_item, current_time)
        return True

    @synchronous('_lock')
    def get_media_item_attr(self, attr, default=None):
        if self._media_item:
            return self._media_item.get_media_item_attr(attr, default)
        return default

    @synchronous('_lock')
    def set_streams(self, audio_uid, sub_uid):
        if not self._media_item or not self._mpv_alive:
            return
        if not self._media_item.is_transcode:
            try:
                if audio_uid is not None:
                    log.debug("PlayerManager::play selecting audio stream index=%s" % audio_uid)
                    self._player.audio = self._media_item.audio_seq[audio_uid]

                if sub_uid == '0':
                    log.debug("PlayerManager::play selecting subtitle stream (none)")
                    self._player.sub = 'no'
                elif sub_uid is not None:
                    log.debug("PlayerManager::play selecting subtitle stream index=%s" % sub_uid)
                    if sub_uid in self._media_item.subtitle_seq:
                        self._player.sub = self._media_item.subtitle_seq[sub_uid]
                    else:
                        log.debug("PlayerManager::play selecting external subtitle id=%s" % sub_uid)
                        self.load_external_sub(sub_uid)
            except _mpv_errors:
                self._handle_mpv_disconnect()
                return

        self._media_item.set_streams(audio_uid, sub_uid)

        if self._media_item.is_transcode:
            self.restart_playback()
        self.timeline_handle()
    
    @synchronous('_lock')
    def load_external_sub(self, sub_id):
        try:
            if sub_id in self.external_subtitles:
                self._player.sub = self.external_subtitles[sub_id]
            else:
                sub_url = self._media_item.get_external_sub(sub_id)
                if settings.log_decisions:
                    log.debug("Load External Subtitle: {0}".format(sub_url))
                self._player.sub_add(sub_url)
                self.external_subtitles[sub_id] = self._player.sub
                self.external_subtitles_rev[self._player.sub] = sub_id
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except SystemError:
            log.debug("PlayerManager::could not load external subtitle")

    def get_track_ids(self):
        if not self._media_item or not self._mpv_alive:
            return None, None
        if self._media_item.is_transcode:
            return self._media_item.get_transcode_streams()
        else:
            aid, sid = None, None
            try:
                if self._player.sub and self._player.sub != 'no':
                    if self._player.sub in self.external_subtitles_rev:
                        sid = self.external_subtitles_rev.get(self._player.sub, '')
                    else:
                        sid = self._media_item.subtitle_uid.get(self._player.sub, '')

                if self._player.audio != 'no':
                    aid = self._media_item.audio_uid.get(self._player.audio, '')
            except _mpv_errors:
                self._handle_mpv_disconnect()
            return aid, sid

    def update_subtitle_visuals(self, restart_transcode=True):
        if not self._media_item or not self._mpv_alive:
            return
        if self._media_item.is_transcode:
            if restart_transcode:
                self.restart_playback()
        else:
            try:
                self._player.sub_pos = SUBTITLE_POS[settings.subtitle_position]
                self._player.sub_scale = settings.subtitle_size / 100
                self._player.sub_color = settings.subtitle_color
            except _mpv_errors:
                self._handle_mpv_disconnect()
                return
        self.timeline_handle()

    def upd_player_hide(self):
        if not self._media_item or not self._mpv_alive:
            return
        try:
            if self._playlist_has_items:
                # mpv manages the playlist; keep_open would prevent it from
                # advancing automatically to the next entry.
                self._player.keep_open = False
            else:
                self._player.keep_open = self._media_item.parent.has_next
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def _handle_mpv_disconnect(self):
        if not self._mpv_alive:
            return
        log.info("MPV connection lost, marking as dead for reconnect on next play.")
        self._media_item = None
        self._mpv_alive = False

    def enable_osc(self, enabled):
        if settings.mpv_ext and settings.mpv_ext_no_ovr:
            return
        try:
            if hasattr(self._player, "osc"):
                self._player.osc = enabled
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def is_active(self):
        return bool(self._player and self._media_item and self._mpv_alive)

    def has_media(self):
        return self._media_item is not None

    def playback_is_aborted(self):
        if not self._mpv_alive:
            return True
        try:
            return self._player.playback_abort
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return True

    def get_playback_time(self):
        if not self._mpv_alive:
            return None
        try:
            return self._player.playback_time
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return None

    def terminate(self):
        self.stop()
        if is_using_ext_mpv and self._mpv_alive:
            try:
                self._player.terminate()
            except Exception:
                log.debug("Error terminating mpv.", exc_info=True)

def _start_stream_proxy(player_mgr):
    """Start a tiny local HTTP proxy for M3U8 playlist entries.

    Each entry in the M3U8 file points to ``http://127.0.0.1:{port}/stream/{pos}``.
    When mpv requests one of those URLs this proxy:

      1. Waits 250 ms so the Plex server has time to close the previous
         episode's streaming session (otherwise Plex returns HTTP 503
         "session still active / max concurrent streams").
      2. Calls ``ep_video.get_playback_url()`` to produce a fresh
         authenticated Plex stream URL using the current session token.
      3. Returns an HTTP 302 redirect so mpv plays the real Plex stream.

    The proxy threads are daemon threads and use a random OS-assigned port
    so they don't interfere with any other service.

    Returns the port number the proxy is listening on.
    """
    from http.server import BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn, TCPServer
    import re as _re, time as _time

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            m = _re.match(r'/stream/(\d+)$', self.path)
            if m:
                pos = int(m.group(1))
                # Give Plex time to close the previous episode's session before
                # opening a new one (avoids HTTP 503 "session still active").
                _time.sleep(0.25)
                ep_video = player_mgr._playlist_items.get(pos)
                if not ep_video:
                    # The dict update phase may still be in flight on a
                    # very fast machine; wait briefly and retry once.
                    _time.sleep(0.5)
                    ep_video = player_mgr._playlist_items.get(pos)
                if ep_video:
                    try:
                        fresh = ep_video.get_playback_url()
                    except Exception:
                        fresh = None
                    if fresh:
                        self.send_response(302)
                        self.send_header('Location', fresh)
                        self.end_headers()
                        return
            self.send_response(503)
            self.end_headers()

        def log_message(self, *args):
            pass  # suppress HTTP access-log noise in the shim's output

    class _Server(ThreadingMixIn, TCPServer):
        allow_reuse_address = True
        daemon_threads = True  # proxy threads won't block process shutdown

    srv = _Server(('127.0.0.1', 0), _Handler)
    port = srv.server_address[1]
    t = Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log.info("_stream_proxy: playlist proxy started on 127.0.0.1:%d" % port)
    return port


playerManager = PlayerManager()
