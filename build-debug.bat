@echo off
git pull
rd /s /q __pycache__ dist build
pyinstaller -cF --add-binary "mpv-2.dll;." --add-binary "plex_mpv_shim\systray.png;." --icon media.ico run.py --hidden-import pystray._win32
