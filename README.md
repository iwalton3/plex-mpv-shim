# MPV Shim for Plex

MPV Shim is a simple and lightweight Plex client, with support for Windows
and Linux. Think of it as an open source Chromecast for Plex. You can cast almost
anything from Plex and it will Direct Play. Subtitles are fully supported, and
there are tools to manage them like no other Plex client.

## Getting Started

If you are on Windows, simply [download the binary](https://github.com/iwalton3/plex-mpv-shim/releases).
If you are using Linux or OSX, please see the [Linux Installation](https://github.com/iwalton3/plex-mpv-shim/blob/master/README.md#linux-installation) or [OSX Installation](https://github.com/iwalton3/plex-mpv-shim/blob/master/README.md#osx-installation) sections below.

To use the client, simply launch it and cast your media from another Plex application.
The mobile and web applications are supported. You do not have to log in to the client
or set it up in any other way.

If you want to use the web application to cast, you must either be on the same network as
a Plex server (an empty one will work) or install the [MPV Shim Local Connection](https://greasyfork.org/en/scripts/398049-mpv-shim-local-connection) user script.

The application runs with a notification icon by default. You can use this to view the application log, open the config folder, and open the application menu.

## Advanced Features

### Menu

To open the menu, press **c** on your computer or **home** within the Plex mobile apps.

The menu enables you to:
 - Adjust video transcoding quality.
 - Change the default transcoder settings.
 - Change subtitles or audio, while knowing the track names.
 - Change subtitles or audio for an entire series at once.
 - Mark the media as unwatched and quit.

On your computer, use the arrow keys, enter, and escape to navigate. On your phone, use
the arrow buttons, ok, back, and home to navigate. (The option for remote controls is
shown next to the name of the client when you select it from the cast menu.)

Please also note that the on-screen controller for MPV (if available) cannot change the
audio and subtitle track configurations for transcoded media. It also cannot load external
subtitles. You must either use the menu or the application you casted from.

### Keyboard Shortcuts

This program supports most of the [keyboard shortcuts from MPV](https://mpv.io/manual/stable/#interactive-control). The custom keyboard shortcuts are:

 - < > to skip episodes
 - q to close player
 - w to mark watched and skip
 - u to mark unwatched and quit
 - c to open the menu

Here are the notable MPV keyboard shortcuts:

 - space - Pause/Play
 - left/right - Seek by 5 seconds
 - up/down - Seek by 1 minute
 - s - Take a screenshot
 - S - Take a screenshot without subtitles
 - f - Toggle fullscreen
 - ,/. - Seek by individual frames
 - \[/\] - Change video speed by 10%
 - {/} - Change video speed by 50%
 - backspace - Reset speed
 - m - Mute
 - d - Enable/disable deinterlace
 - Ctrl+Shift+Left/Right - Adjust subtitle delay.

## Configuration

The configuration file is located in different places depending on your platform. You can open the
configuration folder using the systray icon. When you launch the program on Linux or OSX from the terminal,
the location of the config file will be printed. The locations are:
 - Windows - `%appdata%\plex-mpv-shim\conf.json`
 - Linux - `~/.config/plex-mpv-shim/conf.json`
 - Mac OSX - `Library/Application Support/plex-mpv-shim/conf.json`
 - CygWin - `~/.config/plex-mpv-shim/conf.json`

You can specify a custom configuration folder with the `--config` option.

### Transcoding

You can adjust the basic transcoder settings via the menu.

- `always_transcode` - This will tell the client to always transcode, without asking. Default: `false`
    - This may be useful if you are using limited hardware that cannot handle advanced codecs.
    - You may have some luck changing `client_profile` in the configuration to a more restrictive one.
- `auto_transcode` - This will ask the server to determine if transcoding is suggested. Default: `true`
    - `transcode_kbps` - Transcode bandwidth to request. Default: `2000`
    - `direct_limit` - Also limit direct play to `transcode_kbps`. Default: `false`
    - Note that `direct_limit` cannot be overriden without changing `transcode_kbps`.
    - If `direct_limit` is not set, the player assumes the server will set the limit.
- `adaptive_transcode` - Tell the server to adjust the quality while streaming. Default: `false`

### Shell Command Triggers

You can execute shell commands on media state using the config file:

 - `media_ended_cmd` - When all media has played.
 - `pre_media_cmd` - Before the player displays. (Will wait for finish.)
 - `stop_cmd` - After stopping the player.
 - `idle_cmd` - After no activity for `idle_cmd_delay` seconds.

### Subtitle Visual Settings

All of these settings apply to direct play and are adjustable through the controlling app. Note that some may not work depending on the subtitle codec. Subtitle position and color are not available for transcodes.

 - `subtitle_size` - The size of the subtitles, in percent. Default: `100`
 - `subtitle_color` - The color of the subtitles, in hex. Default: `#FFFFFFFF`
 - `subtitle_position` - The position (top, bottom, middle). Default: `bottom`

### External MPV

The client now supports using an external copy of MPV, including one that is running prior to starting
the client. This may be useful if your distribution only provides MPV as a binary executable (instead
of as a shared library), or to connect to MPV-based GUI players. Please note that SMPlayer exhibits
strange behaviour when controlled in this manner. External MPV is currently the only working backend
for media playback on OSX.

- `mpv_ext` - Enable usage of the external player by default. Default: `false`
    - The external player may still be used by default if `libmpv1` is not available.
- `mpv_ext_path` - The path to the `mpv` binary to use. By default it uses the one in the PATH. Default: `null`
    - If you are using Windows, make sure to use two backslashes. Example: `C:\\path\\to\\mpv.exe`
- `mpv_ext_ipc` - The path to the socket to control MPV. Default: `null`
    - If unset, the socket is a randomly selected temp file.
    - On Windows, this is just a name for the socket, not a path like on Linux.
- `mpv_ext_start` - Start a managed copy of MPV with the client. Default: `true`
    - If not specified, the user must start MPV prior to launching the client.
    - MPV must be launched with `--input-ipc-server=[value of mpv_ext_ipc]`.

### Keyboard Shortcuts

You can reconfigure the custom keyboard shortcuts. You can also set them to `null` to disable the shortcut. Please note that disabling keyboard shortcuts may make some features unusable. Additionally, if you remap `q`, using the default shortcut will crash the player.

 - `kb_stop` - Stop playback and close MPV. (Default: `q`)
 - `kb_prev` - Go to the previous video. (Default: `<`)
 - `kb_next` - Go to the next video. (Default: `>`)
 - `kb_watched` - Mark the video as watched and skip. (Default: `w`)
 - `kb_unwatched` - Mark the video as unwatched and quit. (Default: `u`)
 - `kb_menu` - Open the configuration menu. (Default: `c`)
 - `kb_menu_esc` - Leave the menu. Exits fullscreen otherwise. (Default: `esc`)
 - `kb_menu_ok` - "ok" for menu. (Default: `enter`)
 - `kb_menu_left` - "left" for menu. Seeks otherwise. (Default: `left`)
 - `kb_menu_right` - "right" for menu. Seeks otherwise. (Default: `right`)
 - `kb_menu_up` - "up" for menu. Seeks otherwise. (Default: `up`)
 - `kb_menu_down` - "down" for menu. Seeks otherwise. (Default: `down`)
 - `kb_pause` - Pause. Also "ok" for menu. (Default: `space`)
 - `kb_debug` - Trigger `pdb` debugger. (Default: `~`)
 - `seek_up` - Time to seek for "up" key. (Default: `60`)
 - `seek_down` - Time to seek for "down" key. (Default: `-60`)
 - `seek_right` - Time to seek for "right" key. (Default: `5`)
 - `seek_left` - Time to seek for "left" key. (Default: `-5`)

### Other Configuration Options

 - `player_name` - The name of the player that appears in the cast menu. Initially set from your hostname.
 - `http_port` - The TCP port to listen on for Plex to control the player. Default: `3000`
 - `enable_play_queue` - Enable play queue support. Default: `true`
    - If you disable this, the application will queue media based on the series.
    - This is a legacy feature. It is not regularly tested.
 - `client_uuid` - The identifier for the client. Set to a random value on first run.
 - `audio_ac3passthrough` - Does not work. Currently only changes transcoder settings. Default: `false`
 - `audio_dtspassthrough` - Does not work. Currently only changes transcoder settings. Default: `false`
 - `allow_http` - Allow insecure Plex server connections. Default: `false`
    - This may be useful if you are using a Plex server offline or not signed in.
 - `client_profile` - The client profile for transcoding. Default: `Plex Home Theater`
    - It may be useful to change this on limited hardware.
    - If you change this, it should be changed to a profile that supports `hls` streaming.
 - `sanitize_output` - Prevent Plex tokens from being printed to the console. Default: `true`
 - `fullscreen` - Fullscreen the player when starting playback. Default: `true`
 - `enable_gui` - Enable the system tray icon and GUI features. Default: `true`
 - `media_key_seek` - Use the media next/prev keys to seek instead of skip episodes. Default: `false`
 - `enable_osc` - Enable the MPV on-screen controller. Default: `true`
    - It may be useful to disable this if you are using an external player that already provides a user interface.
 - `log_decisions` - Log the full playback URLs. Default: `false`
 - `mpv_log_level` - Log level to use for mpv. Default: `info`
    - Options: fatal, error, warn, info, v, debug, trace
 - `idle_when_paused` - Consider the player idle when paused. Default: `false`
 - `stop_idle` - Stop the player when idle. (Requires `idle_when_paused`.) Default: `false`
 - `skip_intro_always` - Always skip intros, without asking. Default: `false`
 - `skip_intro_prompt` - Prompt to skip intro via seeking. Default: `true`

### MPV Configuration

You can configure mpv directly using the `mpv.conf` and `input.conf` files. (It is in the same folder as `conf.json`.)
This may be useful for customizing video upscaling, keyboard shortcuts, or controlling the application
via the mpv IPC server.

## Tips and Tricks

Various tips have been found that allow the media player to support special
functionality, albeit with more configuration required.

### Open on Specific Monitor

Please note: Edits to the `mpv.conf` will not take effect until you restart the application. You can open the config directory by using the menu option in the system tray icon.

**Option 1**: Select fullscreen output screen through MPV.
Determine which screen you would like MPV to show up on.
 - If you are on Windows, right click the desktop and select "Display Settings". Take the monitor number and subtract one.
 - If you are on Linux, run `xrandr`. The screen number is the number you want. If there is only one proceed to **Option 2**.

Add the following to your `mpv.conf` in the [config directory](https://github.com/iwalton3/jellyfin-mpv-shim#mpv-configuration), replacing `0` with the number from the previous step:
```
fs=yes
fs-screen=0
```

**Option 2**: (Linux Only) If option 1 does not work, both of your monitors are likely configured as a single "screen".

Run `xrandr`. It should look something like this:

```
Screen 0: minimum 8 x 8, current 3520 x 1080, maximum 16384 x 16384
VGA-0 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 521mm x 293mm
   1920x1080     60.00*+
   1680x1050     59.95  
   1440x900      59.89  
   1280x1024     75.02    60.02  
   1280x960      60.00  
   1280x800      59.81  
   1280x720      60.00  
   1152x864      75.00  
   1024x768      75.03    70.07    60.00  
   800x600       75.00    72.19    60.32    56.25  
   640x480       75.00    59.94  
LVDS-0 connected 1600x900+1920+180 (normal left inverted right x axis y axis) 309mm x 174mm
   1600x900      59.98*+
```

If you want MPV to open on VGA-0 for instance, add the following to your `mpv.conf` in the [config directory](https://github.com/iwalton3/jellyfin-mpv-shim#mpv-configuration):
```
fs=yes
geometry=1920x1080+0+0
```
**Option 3**: (Linux Only) If your window manager supports it, you can tell the window manager to always open on a specific screen.

 - For OpenBox: https://forums.bunsenlabs.org/viewtopic.php?id=1199
 - For i3: https://unix.stackexchange.com/questions/96798/i3wm-start-applications-on-specific-workspaces-when-i3-starts/363848#363848

### Control Volume with Mouse Wheel

Add the following to `input.conf`:
```
WHEEL_UP add volume 5
WHEEL_DOWN add volume -5
```

### MPRIS Plugin

Set `mpv_ext` to `true` in the config. Add `script=/path/to/mpris.so` to `mpv.conf`.

### Run Multiple Instances

You can pass `--config /path/to/folder` to run another copy of the player. Make sure to change the port that the player uses.

### Audio Passthrough

You can edit `mpv.conf` to support audio passthrough. A [user on Reddit](https://reddit.com/r/jellyfin/comments/fru6xo/new_cross_platform_desktop_client_jellyfin_mpv/fns7vyp) had luck with this config:
```
audio-spdif=ac3,dts,eac3 # (to use the passthrough to receiver over hdmi)
audio-channels=2 # (not sure this is necessary, but i keep it in because it works)
af=scaletempo,lavcac3enc=yes:640:3 # (for aac 5.1 tracks to the receiver)
```

### MPV Crashes with "The sub-scale option must be a floating point number or a ratio"

Run the plex-mpv-shim program with LC_NUMERIC=C.

### Use with gnome-mpv/celluloid

You can use `gnome-mpv` with MPV Shim, but you must launch `gnome-mpv` separately before MPV Shim. (`gnome-mpv` doesn't support the MPV command options directly.)

Configure MPV Shim with the following options (leave the other ones):
```json
{
    "mpv_ext": true,
    "mpv_ext_ipc": "/tmp/gmpv-socket",
    "mpv_ext_path": null,
    "mpv_ext_start": false,
    "enable_osc": false
}
```
Then within `gnome-mpv`, click the application icon (top left) > Preferences. Configure the following Extra MPV Options:
```
--idle --input-ipc-server=/tmp/gmpv-socket
```

### Heavy Memory Usage

A problem has been identified where MPV can use a ton of RAM after media has been played,
and this RAM is not always freed when the player goes into idle mode. Some users have
found that using external MPV lessens the memory leak. To enable external MPV on Windows:

 - [Download a copy of MPV](https://sourceforge.net/projects/mpv-player-windows/files/64bit/)
 - Unzip it with 7zip.
 - Configure `mpv_ext` to `true`. (See the config section.)
 - Configure `mpv_ext_path` to `C:\\replace\\with\\path\\to\\mpv.exe`. (Note usage of two `\\`.)
 - Run the program and wait. (You'll probably have to use it for a while.)
 - Let me know if the high memory usage is with `mpv.exe` or the shim itself.

On Linux, the process is similar, except that you don't need to set the `mpv_ext_path` variable.
On OSX, external MPV is already the default and is the only supported player mode.

In the long term, I may look into a method of terminating MPV when not in use. This will require
a lot of changes to the software. 

## Development

If you'd like to run the application without installing it, run `./run.py`.
The project is written entierly in Python 3. There are no closed-source
components in this project. It is fully hackable.

The project is dependent on `python-mpv`, `python-mpv-jsonipc`, and `requests`. If you are using Windows
and would like mpv to be maximize properly, `pywin32` is also needed. The GUI component
uses `pystray` and `tkinter`, but there is a fallback cli mode.

If you are using a local firewall, you'll want to allow inbound connections on
TCP 3000 and UDP 32410, 32412, 32413, and 32414. The TCP port is for the web
server the client uses to recieve commands. The UDP ports are for the [GDM
discovery protocol](https://support.plex.tv/articles/201543147-what-network-ports-do-i-need-to-allow-through-my-firewall/).

This project is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License. The project was ported
to python3, modified to use mpv as the player, and updated to allow all
features of the remote control api for video playback.

## Linux Installation

If you are on Linux, you can install via pip. You'll need [libmpv1](https://github.com/Kagami/mpv.js/blob/master/README.md#get-libmpv) or `mpv` installed.
```bash
sudo pip3 install --upgrade plex-mpv-shim
```
If you would like the GUI and systray features, also install `pystray` and `tkinter`:
```bash
sudo pip3 install pystray
sudo apt install python3-tk
```

You can build mpv from source to get better codec support. Execute the following:
```bash
sudo pip3 install --upgrade python-mpv
sudo apt install autoconf automake libtool libharfbuzz-dev libfreetype6-dev libfontconfig1-dev libx11-dev libxrandr-dev libvdpau-dev libva-dev mesa-common-dev libegl1-mesa-dev yasm libasound2-dev libpulse-dev libuchardet-dev zlib1g-dev libfribidi-dev git libgnutls28-dev libgl1-mesa-dev libsdl2-dev cmake wget python g++ libluajit-5.1-dev
git clone https://github.com/mpv-player/mpv-build.git
cd mpv-build
echo --enable-libmpv-shared > mpv_options
./rebuild -j4
sudo ./install
sudo ldconfig
```

## OSX Installation
Currently on OSX only the external MPV backend seems to be working. I cannot test on OSX, so please report any issues you find.

To install the CLI version:

1. Install brew. ([Instructions](https://brew.sh/))
2. Install python3 and mpv. `brew install python mpv`
3. Install jellyfin-mpv-shim. `pip3 install --upgrade plex-mpv-shim`
4. Run `plex-mpv-shim`.

If you'd like to install the GUI version, you need a working copy of tkinter.

1. Install pyenv. ([Instructions](https://medium.com/python-every-day/python-development-on-macos-with-pyenv-2509c694a808))
2. Install TK and mpv. `brew install tcl-tk mpv`
3. Install python3 with TK support. `FLAGS="-I$(brew --prefix tcl-tk)/include" pyenv install 3.8.1`
4. Set this python3 as the default. `pyenv global 3.8.1`
5. Install jellyfin-mpv-shim and pystray. `pip3 install --upgrade plex-mpv-shim pystray`
6. Run `plex-mpv-shim`.

## Building on Windows

There is a prebuilt version for Windows in the releases section. When
following these directions, please take care to ensure both the python
and libmpv libraries are either 64 or 32 bit. (Don't mismatch them.)

1. Install [Python3](https://www.python.org/downloads/) with PATH enabled. Install [7zip](https://ninite.com/7zip/).
2. After installing python3, open `cmd` as admin and run `pip install --upgrade pyinstaller python-mpv requests pywin32 pystray python-mpv-jsonipc`.
3. Download [libmpv](https://sourceforge.net/projects/mpv-player-windows/files/libmpv/).
4. Extract the `mpv-1.dll` from the file and move it to the `plex-mpv-shim` folder.
5. Open a regular `cmd` prompt. Navigate to the `plex-mpv-shim` folder.
6. Run `pyinstaller -wF --add-binary "mpv-1.dll;." --add-binary "plex_mpv_shim\systray.png;." --icon media.ico run.py`.
