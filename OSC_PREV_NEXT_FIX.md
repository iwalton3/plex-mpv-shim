# ModernZ OSC — Previous/Next Track Buttons Fix

## Summary

The previous/next track buttons in the ModernZ OSC did not navigate episodes
when playing Plex content through `mpv-shim`, even though the equivalent
keyboard shortcuts (`<` and `>`) did. Two separate issues were identified and
the click commands were re-routed to fix the behavior.

## Symptoms

- Pressing `<` or `>` on the keyboard → next/previous episode played correctly.
- Clicking the outer ⏮ / ⏭ buttons in the OSC → button highlighted on hover
  but clicking did nothing.
- Diagnostic on-screen display confirmed the click was firing and mpv saw
  `playlist-pos-1 = 1`, `playlist-count = 1`.

## Root cause

The setup is mpv.exe driven by **mpv-shim for Plex**. The shim does not push
the Plex episode queue into mpv's playlist — mpv only ever sees the single
currently-playing episode (hence `pos 1/1`). When the user presses `>`, the
shim intercepts that key event via a forced key binding and asks the Plex API
for the next episode, then loads it.

The ModernZ OSC button, by contrast, was wired to call `mp.command("playlist-next")`
directly inside mpv. That command goes straight to mpv core, which has a
1-item playlist and therefore has nowhere to advance to. The shim never sees
this command — it only listens for the key event, not for the
`playlist-next` command itself.

A secondary issue was found in `modernz.conf`: the playlist button click
commands were declared twice in the file. mpv's options parser uses the
**last** value, so an earlier attempt at a fix (`keypress PREV` / `keypress NEXT`)
was being silently overridden by the default `playlist-next` / `playlist-prev`
later in the same file.

## Why hotkeys worked but buttons did not

Both `>` and the OSC button were nominally trying to do the same thing —
advance to the next episode — but they went through different code paths:

| Action | Path | Result |
| --- | --- | --- |
| Press `>` | input.conf → key event → mpv-shim's forced binding → Plex API → load next episode | Works |
| Click ⏭ | ModernZ click handler → `mp.command("playlist-next")` → mpv core → playlist length 1 → no-op | Does nothing |

The shim only registers handlers for the **input event** of the key. Calling
the underlying command directly from a Lua script bypasses the shim entirely.

## Code changes

### File 1: `portable_config/script-opts/modernz.conf`

**Change 1** — removed a stale duplicate block of playlist-button commands
that was being silently overridden later in the file. This was at lines
377–379.

Before:

```ini
# playlist button mouse actions
playlist_prev_mbtn_left_command=keypress PREV
playlist_next_mbtn_left_command=keypress NEXT
```

After:

```ini
# playlist button mouse actions
# (defined further down in "playlist skip buttons mouse actions" section)
```

**Change 2** — re-routed the OSC button clicks to simulate the same key press
that the shim already watches for. This is in the "playlist skip buttons
mouse actions" section (around lines 417–423).

Before:

```ini
playlist_prev_mbtn_left_command=playlist-prev
playlist_prev_mbtn_mid_command=show-text ${playlist} 3000
playlist_prev_mbtn_right_command=script-binding select/select-playlist

playlist_next_mbtn_left_command=playlist-next
playlist_next_mbtn_mid_command=show-text ${playlist} 3000
playlist_next_mbtn_right_command=script-binding select/select-playlist
```

After:

```ini
playlist_prev_mbtn_left_command=keypress <
playlist_prev_mbtn_mid_command=show-text ${playlist} 3000
playlist_prev_mbtn_right_command=script-binding select/select-playlist

playlist_next_mbtn_left_command=keypress >
playlist_next_mbtn_mid_command=show-text ${playlist} 3000
playlist_next_mbtn_right_command=script-binding select/select-playlist
```

The `keypress <` / `keypress >` commands generate the same input event mpv
receives when the keys are physically pressed, so the shim's forced binding
fires exactly as it does for the keyboard shortcut.

### Files left unchanged

- `portable_config/input.conf` — no change needed. The existing bindings for
  `<` and `>` (lines 196–197) are what the OSC buttons now route through.
- `portable_config/scripts/modernz.lua` — no change needed. The button
  enable/visible state was already correct because
  `buttons_always_active=playlist_prev,playlist_next` is set in `modernz.conf`
  (line 185), keeping the eventresponder alive even with a 1-item playlist.

## How to verify

1. Fully close every mpv window (`script-opts` are read at startup).
2. Start an episode through Plex / mpv-shim.
3. Hover over the outer ⏮ / ⏭ buttons — they should highlight.
4. Click ⏭ → next episode should load. Click ⏮ → previous episode should
   load. Behavior should now match pressing `<` / `>` on the keyboard.

## Related observations

- The middle ↺10 / 10↻ buttons are `jump_backward` / `jump_forward` and seek
  ±10 seconds within the current episode. They are not playlist navigation
  and were never affected.
- The chapter prev/next buttons are hidden in this config
  (`chapter_skip_buttons=no`). If they are ever re-enabled they will not have
  this problem because chapters live inside the single loaded file.
- Middle-click on the playlist buttons still shows `${playlist}`, which under
  mpv-shim will always be a 1-item list. This is a shim limitation, not a
  config bug.

## Optional future improvement (shim side)

A cleaner solution that would benefit any OSC would be for mpv-shim to
register its next/previous-episode actions as **named script-bindings**
(`mp.add_key_binding(None, 'next-episode', handler)`) in addition to its raw
key bindings. The OSC could then call `script-binding mpv-shim/next-episode`
directly, removing the dependency on `<` / `>` being bound. This is a small
change in the shim's Python source and is worth filing as an issue on the
relevant `plex-mpv-shim` / `jellyfin-mpv-shim` repo if the current workaround
becomes inconvenient.

Putting the full Plex queue into mpv's playlist is also possible but is a
more invasive change — Plex stream URLs are session-tokenized and tied to a
transcoding session per episode, so pre-loading would require lazy URL
re-resolution, transcoding session management, and reconciling against
Plex's dynamic "up next" recommendation. The `keypress` workaround is
strictly simpler.
