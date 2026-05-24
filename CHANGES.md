# plex-mpv-shim playlist fixes — development log

## Goal

Extend plex-mpv-shim 1.12.0 so that:

- The **full current season** is visible in mpv's native OSC playlist when playing a TV episode
- **Previous episodes** (before the one you opened) are included and navigable
- **Backward navigation** works after having gone forward (e.g. ep3 → ep4 → back to ep3)
- No HTTP 503 errors when switching episodes
- No episode restart/re-buffer when the playlist finishes loading in the background
- Skipping or jumping to an episode does **not** mark it as watched in Plex

---

## Files changed

| File | What changed |
|------|-------------|
| `plex_mpv_shim/media.py` | `_expand_via_all_leaves()` rewritten; `upd_play_queue()` now always expands for TV |
| `plex_mpv_shim/player.py` | New playlist state variables, proxy server, `_populate_playlist()` rewritten, `_on_playlist_advance_inner()` added, `_ep_finished_naturally` flag |

---

## Bug 1 — No season episodes in the OSC playlist

**Root cause**

Plex's play queue only includes the selected episode and those that come after it. When `Media.upd_play_queue()` built `parent.series` from the play queue, it only ever saw future episodes. `parent.seq` was always `0` because the selected episode was always first in the queue.

`_populate_playlist` used `parent.seq` as `start_seq` and `len(parent.series)` as `total`, so the resulting M3U8 only contained episodes from the starting point onwards — episodes before it simply did not exist in the data.

**Fix — `media.py` `upd_play_queue()`**

After loading the play queue, always call `_expand_via_all_leaves()` for TV shows:

```python
# Always expand to full season for TV shows so that episodes
# before the starting point are included in the playlist and
# backward navigation works correctly.
if self.is_tv:
    self._expand_via_all_leaves()
```

Previously this fallback only triggered when the play queue had ≤ 1 item. A queue starting at episode 10 of a season still has 10+ items, so the condition never fired.

---

## Bug 2 — Previous episodes not accessible / pressing prev closed mpv

**Root cause**

A direct consequence of Bug 1. With `seq = 0` there was nothing at a position before 0 in the mpv playlist, so `playlist-prev` at position 0 caused mpv to close entirely (nothing to go back to).

**Fix**

Same as Bug 1 — once `_expand_via_all_leaves()` correctly sets `seq = 9` for the 10th episode of a season, for example, positions 0–8 exist in the M3U8 and `playlist-prev` works normally.

---

## Bug 3 — Going backward after going forward didn't work (503 errors)

**Root cause**

When navigating between episodes, the M3U8 entries contained real Plex URLs generated at playlist-build time. When the user navigated away from an episode and then tried to return, Plex had already closed that streaming session. The stored URL was now stale and returned HTTP 503 "session still active / max concurrent streams".

**Fix — local stream proxy in `player.py`**

All M3U8 entries now point to a local HTTP proxy (`http://127.0.0.1:{port}/stream/{pos}`) instead of Plex directly. The proxy:

1. Waits 250 ms to let Plex close the previous episode's session
2. Calls `ep_video.get_playback_url()` fresh to get a new authenticated URL
3. Returns an HTTP 302 redirect to mpv

```python
def do_GET(self):
    m = _re.match(r'/stream/(\d+)$', self.path)
    if m:
        pos = int(m.group(1))
        _time.sleep(0.25)   # let Plex close previous session
        ep_video = player_mgr._playlist_items.get(pos)
        if ep_video:
            fresh = ep_video.get_playback_url()
            if fresh:
                self.send_response(302)
                self.send_header('Location', fresh)
                self.end_headers()
                return
    self.send_response(503)
    self.end_headers()
```

Every navigation — forward, backward, or jump — always gets a valid fresh URL.

---

## Bug 4 — Whole show loaded instead of just the current season

**Root cause**

`_expand_via_all_leaves()` used `grandparentKey + "/allLeaves"`. In Plex's data model:

- `grandparentKey` = the **show** (e.g. `/library/metadata/9623`)
- `parentKey` = the **season** (e.g. `/library/metadata/9624`)

`/allLeaves` on the show key returns every episode across all seasons. A show with 4 seasons returned 79+ episodes, making the initial metadata fetch slow and the OSC playlist cluttered.

**Fix — `media.py` `_expand_via_all_leaves()`**

Switch from `grandparentKey + "/allLeaves"` to `parentKey + "/children"`:

```python
# Before (whole show, all seasons):
series_xml = XMLCollection(self.get_path(grandparent_key + "/allLeaves"))

# After (current season only):
series_xml = XMLCollection(self.get_path(parent_key + "/children"))
```

The `/children` endpoint on a season key returns only the episodes of that season. No specials-filtering is needed since other seasons' episodes don't appear in a specific season's children list.

---

## Bug 5 — Episode restarted from the beginning when the playlist finished loading

**Root cause**

`_populate_playlist` used `loadlist replace` to swap in the full season M3U8. `loadlist replace` **stops the currently playing stream**, then starts the M3U8 from position 0. The code then called `set playlist-pos {start_seq}` to jump to the right episode, but by then mpv had already terminated the stream and was opening episode 1. The user saw a visible restart (re-buffer from the beginning of the current episode).

**Fix — append + playlist-move in `player.py` `_populate_playlist()`**

The new strategy never calls `loadlist replace`. Instead:

**Past episodes (before the starting point):**
1. Write a separate M3U8 for episodes 0 to `start_seq - 1`
2. `loadlist append` — these land at mpv positions 1, 2, … n (after the current episode at pos 0)
3. Issue `playlist-move k+1 k` for each past episode to shift it before the current one

After n moves the current episode has shifted from position 0 to position `start_seq` without ever stopping playback:

```
Before:  [current(0), ep0(1), ep1(2), ep2(3)]
Move 1→0: [ep0(0), current(1), ep1(2), ep2(3)]
Move 2→1: [ep0(0), ep1(1), current(2), ep2(3)]
Move 3→2: [ep0(0), ep1(1), ep2(2), current(3)]  ← start_seq = 3
```

**Future episodes (after the starting point):**
- Write a second M3U8 for episodes `start_seq + 1` to `total - 1`
- `loadlist append` — these land immediately after the current episode

The currently playing episode is never touched. No restart, no re-buffer.

The `_playlist_expected_pos = start_seq` guard suppresses spurious `_on_playlist_advance` events while the current episode shifts from pos 0 to pos `start_seq` during the move loop.

---

## Bug 6 — Skipping or jumping marked episodes as watched

**Root cause**

`_on_playlist_advance_inner` called `set_played()` on the previous episode whenever `playlist-pos` changed — including when the user manually clicked a different episode in the OSC playlist, pressed the next/prev hotkeys, or jumped non-sequentially. There was no check for whether the episode had actually been watched.

`finished_callback` (which is the correct place for `set_played()`) returns early when the managed playlist is active, so it was not doing this work — `_on_playlist_advance_inner` was the only caller, and it fired for every transition indiscriminately.

**Fix — `_ep_finished_naturally` flag in `player.py`**

A new boolean flag tracks whether the current episode reached a genuine end-of-file:

```python
# In __init__ and _play_media reset block:
self._ep_finished_naturally = False

# In the eof-reached property observer:
@self._player.property_observer('eof-reached')
def handle_end(_name, reached_end):
    if self._media_item and reached_end:
        self._ep_finished_naturally = True   # ← natural end
        ...

# In _on_playlist_advance_inner:
if self._media_item and ep_video is not self._media_item:
    if self._ep_finished_naturally:
        self._media_item.set_played()    # only if actually finished
    # else: skip — user jumped/skipped, episode stays unwatched
self._ep_finished_naturally = False      # reset for next episode
```

`eof-reached = True` only fires when mpv plays a file to its natural end. Manual navigation changes `playlist-pos` without triggering `eof-reached`, so `_ep_finished_naturally` stays `False` and `set_played()` is never called for skipped episodes.

---

## Summary of all changes

### `media.py`

- **`_expand_via_all_leaves()`**: Rewritten to use `parentKey + "/children"` (season only) instead of `grandparentKey + "/allLeaves"` (entire show). Specials-filtering logic removed (not needed for a single season endpoint).
- **`upd_play_queue()`**: Removed the `len(self.series) <= 1` guard — `_expand_via_all_leaves()` is now called for every TV show unconditionally, ensuring the full current season (with correct `seq`) is always available.

### `player.py`

- **`PlayerManager.__init__`**: Added `_ep_finished_naturally = False` to instance variables.
- **`_play_media` reset block**: Added `self._ep_finished_naturally = False`.
- **`handle_end` (eof-reached observer)**: Sets `self._ep_finished_naturally = True` when a natural end-of-file is detected.
- **`_populate_playlist()`**: Completely rewritten — no longer uses `loadlist replace`. Uses `loadlist append` for past episodes followed by `playlist-move` commands, then `loadlist append` for future episodes. Current episode continues playing uninterrupted throughout.
- **`_on_playlist_advance_inner()`**: `set_played()` now gated on `_ep_finished_naturally`. Flag is cleared after each transition.
- **`_start_stream_proxy()`**: Simplified — removed `_playlist_start_seq` conditional delay (was only needed to compensate for the now-removed replace approach). Proxy always waits 250 ms before fetching a fresh URL.
