#!/usr/bin/env python3

import logging
import sys
import time
import os.path
import multiprocessing

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s")

from . import conffile
from .conf import settings
from .gdm import gdm

HTTP_PORT   = 3000
APP_NAME = 'plex-mpv-shim'

log = logging.getLogger('')

logging.getLogger('requests').setLevel(logging.CRITICAL)

def update_gdm_settings(name=None, value=None):
    gdm.clientDetails(settings.client_uuid, settings.player_name,
        settings.http_port, "Plex MPV Shim", "1.0")

def main():
    conf_file = conffile.get(APP_NAME,'conf.json')
    if os.path.isfile('settings.dat'):
        settings.migrate_config('settings.dat', conf_file)
    settings.load(conf_file)
    settings.add_listener(update_gdm_settings)
    
    if sys.platform.startswith("darwin"):
        multiprocessing.set_start_method('forkserver')

    use_gui = False
    if settings.enable_gui:
        try:
            from .gui_mgr import userInterface
            use_gui = True
        except Exception:
            log.warning("Cannot load GUI. Falling back to command line interface.", exc_info=1)

    if not use_gui:
        from .cli_mgr import userInterface

    from .player import playerManager
    from .timeline import timelineManager
    from .action_thread import actionThread
    from .client import HttpServer

    update_gdm_settings()
    gdm.start_all()

    log.info("Started GDM service")

    server = HttpServer(int(settings.http_port))
    server.start()

    timelineManager.start()
    playerManager.timeline_trigger = timelineManager.trigger
    actionThread.start()
    playerManager.action_trigger = actionThread.trigger
    userInterface.open_player_menu = playerManager.menu.show_menu

    try:
        userInterface.run()
    except KeyboardInterrupt:
        print("")
        log.info("Stopping services...")
    finally:
        playerManager.terminate()
        server.stop()
        timelineManager.stop()
        actionThread.stop()
        gdm.stop_all()

if __name__ == "__main__":
    main()

