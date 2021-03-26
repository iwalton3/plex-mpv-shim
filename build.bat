@echo off
git pull
rd /s /q __pycache__ dist build
pyinstaller -wF --add-binary "mpv-1.dll;." --add-binary "plex_mpv_shim\systray.png;." --add-data "plex_mpv_shim\default_shader_pack;plex_mpv_shim\default_shader_pack" --hidden-import pystray._win32 --icon media.ico run.py
