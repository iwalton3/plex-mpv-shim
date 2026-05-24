# Plex MPV Shim — Bug Fixes

## Fix 1: Shader Pack — Clearer Error on Missing Installation Files

**File:** `plex_mpv_shim/video_profile.py`

### Problem
On startup, if the `default_shader_pack` directory was missing from the installation folder (e.g. after a partial install), the shim crashed deep inside Python's `shutil.copytree` with a confusing error:

```
FileNotFoundError: [WinError 3] The system cannot find the path specified:
'C:\PC Utilities\Plex MPV Shim\_internal\plex_mpv_shim\default_shader_pack'
```

The traceback pointed into `shutil.py` rather than at the actual cause, making it hard to diagnose.

### Fix
Added an existence check at the top of `load_shader_pack()` so it raises a clear, descriptive `FileNotFoundError` before any file operations are attempted.

```python
# Before
def load_shader_pack(self):
    shader_pack_builtin = get_resource("default_shader_pack")

    self.shader_pack = shader_pack_builtin
    if settings.shader_pack_custom:
        self.shader_pack = conffile.get(APP_NAME, "shader_pack")
        if not os.path.exists(self.shader_pack):
            shutil.copytree(shader_pack_builtin, self.shader_pack)

# After
def load_shader_pack(self):
    shader_pack_builtin = get_resource("default_shader_pack")

    if not os.path.exists(shader_pack_builtin):
        raise FileNotFoundError(
            "Built-in shader pack not found at '{0}'. "
            "The default_shader_pack directory is missing from your installation. "
            "Try reinstalling Plex MPV Shim.".format(shader_pack_builtin)
        )

    self.shader_pack = shader_pack_builtin
    if settings.shader_pack_custom:
        self.shader_pack = conffile.get(APP_NAME, "shader_pack")
        if not os.path.exists(self.shader_pack):
            shutil.copytree(shader_pack_builtin, self.shader_pack)
```

### Notes
The shader pack being absent is an installation issue, not a runtime code bug. Shader profiles will be disabled until the pack is restored (reinstalling Plex MPV Shim is the fix). This change just makes the error message point directly at the cause.

---

## Fix 2: Player Deadlock After Stream 404

**File:** `plex_mpv_shim/player.py`

### Problem
**Reproduction steps:** Try to play an episode whose file is missing on the server (404), then try to play any other episode. The second episode also fails to play and the shim must be restarted.

**Root cause:** `_play_media()` called `self._player.wait_for_property("duration")` after issuing the play command to mpv. This call works by watching for the `duration` property to *change*. When a stream returns a 404, mpv errors out immediately and `duration` stays `None` — it never changes, so the event never fires, and `_play_media` blocks forever.

Because `_play_media` is decorated with `@synchronous('_lock')`, it holds the player lock the entire time it is blocked. Every subsequent player operation — stop, next play, anything — queues up behind it. The shim appears to accept commands (URL building happens before the lock is acquired and still logs normally) but never actually acts on them.

### Fix
Replaced `wait_for_property("duration")` with a new `_wait_for_playback_start()` helper that watches both `duration` and `playback-abort` simultaneously, plus a 30-second hard timeout as a last resort.

- **Success path:** mpv sets `duration` to a real value → returns `True` → play continues normally.
- **Failure path:** mpv sets `playback-abort = True` (which it reliably does on stream errors) → returns `False` → `_play_media` exits early *before* setting `_media_item`, releasing the lock immediately.
- **Timeout path:** neither fires within 30 seconds → returns `False` → same early exit.

```python
# New helper method added to PlayerManager
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
```

```python
# In _play_media — before
try:
    self._player.play(self.url)
    self._player.wait_for_property("duration")
    if settings.fullscreen:
        self._player.fs = True
    self._player.force_media_title = media_item.get_proper_title()
except _mpv_errors:
    self._handle_mpv_disconnect()
    return
self._media_item = media_item

# In _play_media — after
try:
    self._player.play(self.url)
except _mpv_errors:
    self._handle_mpv_disconnect()
    return

if not self._wait_for_playback_start():
    # Stream failed (e.g. 404) or timed out. Returning here releases
    # _lock immediately so the next play command is never blocked.
    log.warning("PlayerManager::_play_media stream failed to start, aborting play")
    return

try:
    if settings.fullscreen:
        self._player.fs = True
    self._player.force_media_title = media_item.get_proper_title()
except _mpv_errors:
    self._handle_mpv_disconnect()
    return
self._media_item = media_item
```

### Additional import
`Event` was added to the existing `threading` import at the top of `player.py`:

```python
# Before
from threading import RLock, Lock

# After
from threading import RLock, Lock, Event
```
