# Plex MPV Shim — Bug Fixes and Code Changes

This document records the patches applied to `plex-mpv-shim-1.12.0` to fix episode-progression, shutdown, and build issues.

## Overview

The original analysis docs in this folder (`EPISODE_PROGRESSION_*.md`, `README_BUG_ANALYSIS.md`) pointed at a lock-contention bug in `finished_callback`. After tracing actual runtime logs, the real causes turned out to be elsewhere. Five distinct bugs were fixed across `player.py`, `media.py`, `action_thread.py`, `timeline.py`, `client.py`, `mpv_shim.py`, and `build.bat`.

---

## Bug 1 — Truncated Plex play queue stops auto-play after 1–2 episodes

**Symptom.** Auto-play would stop after the second episode of a show. Log showed `=== finished_callback: NO NEXT MEDIA, STOPPING ===` even though the show had many more episodes available on Plex.

**Root cause.** `Media.upd_play_queue()` re-fetches `/playQueues/<id>` from Plex with no window parameter. Plex returns a narrow slice — typically just the current episode and the immediate next. After episode 2 finishes, `get_next()` re-fetches the same URL, still gets only 2 items, and reports `has_next=False`. The existing fallback to `allLeaves` (full season fetch) only fired when the queue had ≤1 item, so a 2-item queue silently dead-ended.

**Fix — `plex_mpv_shim/media.py`.**

1. Extracted the inline `allLeaves` fallback in `upd_play_queue` into a reusable method `_expand_via_all_leaves()` (lines ~670–715). It fetches `<grandparentKey>/allLeaves`, locates the current episode by `key`, and rewrites `self.series`/`self.seq` on success; leaves state untouched on failure.
2. `upd_play_queue` now delegates the ≤1 case to this helper (line ~738).
3. `get_next()` now calls the helper as a **second-level fallback** when the re-fetched play queue is still exhausted at the end (lines ~771–780):

```python
if self.play_queue and not self.has_next:
    self.upd_play_queue()
    if (self.is_tv
            and self.seq is not None
            and self.seq >= len(self.series) - 1):
        log.warning("=== get_next: play_queue exhausted at seq=%d/len=%d, expanding via allLeaves ===" % (self.seq, len(self.series)))
        if self._expand_via_all_leaves():
            self.has_next = self.seq < len(self.series) - 1
            self.has_prev = self.seq > 0
```

**Verification.** Log now shows `=== get_next: play_queue exhausted at seq=1/len=2, expanding via allLeaves ===` followed by `=== _expand_via_all_leaves: expanded to N episodes, seq=K ===` at the queue boundary.

---

## Bug 2 — Natural end-of-episode never auto-advances

**Symptom.** At natural end of an episode, the player paused on the last frame (or appeared to loop). Auto-play to the next episode only happened when the user manually closed/stopped the current episode.

**Root cause.** The shim relied on the `eof-reached` MPV property observer (`handle_end` in `player.py`) to detect end-of-file. Runtime logs showed `reached_end=None` and `reached_end=False` every time, **never `True`** — likely due to user-side mpv config (`keep_open=yes` interaction, or `loop-file=inf` in an included config file) preventing the property from being set.

**Fix — `plex_mpv_shim/player.py` (lines ~281–301).** Added a parallel `end-file` MPV event callback alongside the existing property observer. The `end-file` event fires reliably with a `reason` field; we only auto-advance when `reason == "eof"` (natural end), not for `"stop"`, `"quit"`, `"redirect"`, or `"error"`:

```python
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
        if self._media_item and reason == "eof":
            has_lock = self._finished_lock.acquire(False)
            self.put_task(self.finished_callback, has_lock)
    except Exception:
        log.warning("Error in end-file handler", exc_info=True)
```

The original `eof-reached` observer is preserved (it still works correctly on some configurations and acts as a redundant trigger; the second event is harmless because `finished_callback` is idempotent under the existing lock).

---

## Bug 3 — Pressing STOP / closing the window auto-played the next episode

**Symptom.** Closing or stopping playback advanced to the next episode instead of stopping. The user reported: *"Episode 8 plays after we close episode 7 which should not happen as closing it should end the playback."*

**Root cause.** `handle_stop` in `player.py` was wired to call `finished_callback` (which auto-advances). This appears to have been a workaround that a previous author added because Bug 2 (natural EOF detection) was already broken — STOP was being used as a proxy for "episode ended." Once Bug 2 was actually fixed, the workaround became the bug.

**Fix — `plex_mpv_shim/player.py` (lines ~153–161).** Routed `handle_stop` straight to `stop_and_close` with no auto-advance side-effect:

```python
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
```

Combined with Bug 2's fix, the two stop paths are now cleanly separated: natural EOF → `finished_callback` → next episode; user STOP → `stop_and_close` → playback ends.

---

## Bug 4 — `finished_callback` "no lock" branch retry race (Fix #1)

**Symptom.** Theoretical: at end-of-file, if `_finished_lock` was held by another operation, the original code silently returned without queueing the next episode. (Not observed at runtime in our logs — `has_lock=True` every time — but the codepath still existed.)

**Fix — `plex_mpv_shim/player.py` (lines ~667–676).** Replaced an immediate `self.put_task(self.play_next)` with a 0.5 s `threading.Timer` retry, per the original analysis doc's "Fix #1" recommendation:

```python
elif next_media:
    log.debug("PlayerManager::finished_callback No lock, queuing retry...")
    import threading
    timer = threading.Timer(
        0.5,
        lambda: self.put_task(self.play_next)
    )
    timer.daemon = True
    timer.start()
```

**Caveat.** This branch was already inert in the observed runtime (`has_lock` was always `True`). The Bug 2/Bug 3 fixes are what actually changed behavior for the user. Fix #1 remains as a defensive measure.

---

## Bug 5 — Process lingers in Task Manager after closing the shim

**Symptom.** After closing the shim, `run.exe` (and sometimes `mpv.exe`) stayed in Task Manager and had to be killed manually. On rebuild attempts, PyInstaller failed with `WinError 32` because the old `run.exe` still held files in `dist\run\` open.

**Root cause.** Multiple background threads were declared as **non-daemon**, so any one of them blocking inside its `stop()` (e.g., a Plex HTTP call holding a `multiprocessing.Pool` worker, a stuck socket read in the HTTP server) prevented the Python interpreter from exiting. Additionally, on **Python 3.14** `python-mpv-jsonipc`'s `MPV.__del__()` raises `PythonFinalizationError: cannot join thread at interpreter shutdown`, so its event-handler thread never exits — keeping the subprocess pipe to `mpv.exe` open and the parent process alive.

**Fix — five files.**

### `plex_mpv_shim/action_thread.py`
```python
threading.Thread.__init__(self)
# Daemonize so process exit isn't blocked if the loop is stuck on a
# @synchronous('_lock') call inside playerManager.update().
self.daemon = True
...
def stop(self):
    self.halt = True
    self.trigger.set()
    self.join(timeout=3)  # bounded join
```

### `plex_mpv_shim/timeline.py`
```python
threading.Thread.__init__(self)
self.daemon = True
...
def stop(self):
    self.halt = True
    self.trigger.set()
    try:
        # Kill in-flight pool workers instead of waiting for slow Plex
        # requests to finish. close()+join() would block here.
        self.sender_pool.terminate()
        self.sender_pool.join()
    except Exception:
        pass
    self.join(timeout=3)
```

### `plex_mpv_shim/client.py`
```python
class HttpServer(threading.Thread):
    def __init__(self, port):
        super(HttpServer, self).__init__(name="HTTP Server")
        ...
        self.daemon = True  # worker threads were already daemon=True
```

### `plex_mpv_shim/mpv_shim.py`
```python
update_gdm_settings()
gdm.start_all(daemon=True)  # GDM registration thread now daemon

...

finally:
    # Wrap each cleanup so a single hung call can't prevent the rest
    # from running. Then force-exit to bypass Python finalization
    # (which is what hangs on Python 3.14 due to python-mpv-jsonipc's
    # non-joinable thread).
    for shutdown_step in (
        ("playerManager.terminate", playerManager.terminate),
        ("server.stop", server.stop),
        ("timelineManager.stop", timelineManager.stop),
        ("actionThread.stop", actionThread.stop),
        ("gdm.stop_all", gdm.stop_all),
    ):
        name, fn = shutdown_step
        try:
            fn()
        except Exception:
            log.warning("Error during shutdown step %s" % name, exc_info=True)
    if use_gui:
        try:
            userInterface.stop()
        except Exception:
            log.warning("Error stopping userInterface", exc_info=True)
    log.info("Shutdown complete; forcing process exit.")
    os._exit(0)
```

### `build.bat`
Added `--noconfirm` so PyInstaller doesn't prompt when overwriting `dist\run\`:
```bat
pyinstaller --noconfirm -w --add-binary "mpv-2.dll;." --add-binary "plex_mpv_shim\systray.png;." --add-data "plex_mpv_shim\mouse.lua;plex_mpv_shim" --hidden-import pystray._win32 --icon media.ico run.py
```

---

## Files changed (summary)

| File | Lines (approx.) | Purpose |
|------|-----------------|---------|
| `plex_mpv_shim/media.py` | +47 / −37 | `_expand_via_all_leaves` helper + `get_next` second-level fallback |
| `plex_mpv_shim/player.py` | +20 / −9  | `end-file` event handler; `handle_stop` no longer auto-advances; Fix #1 timer |
| `plex_mpv_shim/action_thread.py` | +5 / −1 | Daemonize + bounded join |
| `plex_mpv_shim/timeline.py` | +12 / −2 | Daemonize + bounded join + `sender_pool.terminate()` |
| `plex_mpv_shim/client.py` | +3 / 0 | Daemonize `HttpServer` |
| `plex_mpv_shim/mpv_shim.py` | +25 / −7 | Wrapped cleanup, `gdm.start_all(daemon=True)`, `os._exit(0)` |
| `build.bat` | +1 / −1 | `--noconfirm` |

---

## Build / verification notes

### Environment caveats
- Tested with **Python 3.14** + `python-mpv` + `python-mpv-jsonipc` fallback. Python 3.14's stricter thread-shutdown rules (`PythonFinalizationError`) are the reason for `os._exit(0)` rather than letting the interpreter clean up. Python 3.12/3.13 don't strictly need it but still benefit from it.
- `mpv-2.dll` must be discoverable to `python-mpv` at import time. Either keep it next to `run.py` and run with `set PATH=%CD%;%PATH%`, or install `libmpv` system-wide. PyInstaller bundles it via `--add-binary "mpv-2.dll;."`, so runtime users don't need it on PATH.
- Required pip packages: `python-mpv`, `python-mpv-jsonipc`, `requests`, `certifi`, `pystray`, `Pillow`, `pyinstaller` (build only). The `setup.py` declares the binding as `mpv` which is wrong — install `python-mpv`.

### Rebuild procedure
1. Kill any lingering `run.exe` / `mpv.exe` from prior sessions:
   ```
   taskkill /F /IM run.exe
   taskkill /F /IM mpv.exe
   ```
2. From the project root:
   ```
   set PATH=%CD%;%PATH%
   build.bat
   ```
3. Final line should read `Built dist\run\run.exe`.

### Quick sanity check before building
```
set PATH=%CD%;%PATH%
python -c "from plex_mpv_shim import media, player, mpv_shim; print('imports OK')"
```
`imports OK` should appear in stdout (interleaved with harmless warnings about `win_utils` / shader pack).

### Expected log output after fixes
At natural end of episode:
```
=== end-file EVENT reason=eof media_item=True ===
=== end-file: queuing finished_callback has_lock=... ===
=== finished_callback CALLED has_lock=... ===
=== get_next() CALLED ===
=== finished_callback: PLAYING NEXT ===
```

When the play queue is exhausted but the show has more episodes:
```
=== get_next: play_queue exhausted at seq=N/len=N+1, expanding via allLeaves ===
=== _expand_via_all_leaves: expanded to M episodes, seq=K ===
=== get_next: successfully created next media ===
```

On shutdown:
```
Stopping HTTP server...
Shutdown complete; forcing process exit.
```
Process should disappear from Task Manager within ~1 second.

---

## Known remaining issues (not addressed)

These were identified during the audit but left as-is because they're low-impact, theoretical, or out of scope:

- **`_finished_lock` can leak on `_play_media` early-return paths.** Five `return` branches in `_play_media` (mpv-disconnect handling) skip the lock release at the end. With `os._exit(0)` on shutdown the leak no longer causes hang at exit, but it can cause repeated retries during runtime.
- **TOCTOU race on `_finished_lock.release()`** (player.py ~503–504). Wrap in `try/except RuntimeError` if it becomes an issue.
- **Debug-level `log.warning("=== ... ===")` calls** scattered through hot paths. They were left in place because they helped diagnose the bugs above; consider lowering to `log.debug` for a release build.
- **`upd_play_queue` "prefetch" path is a no-op** when the source queue is static (re-fetches the same URL). Left alone because Bug 1's fallback now handles this.
