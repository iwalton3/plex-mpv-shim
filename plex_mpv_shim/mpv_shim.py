#!/usr/bin/env python3

import logging
import sys
import time
import os
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

def setup_file_logging():
    """Setup file logging to the config directory."""
    if settings.enable_debug_log:
        try:
            # Get the config directory
            log_dir = conffile.confdir(APP_NAME)
            log_file = os.path.join(log_dir, 'plex-mpv-shim.log')
            
            # Create file handler
            file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
            
            # Set log level
            log_level = getattr(logging, settings.debug_log_level.upper(), logging.DEBUG)
            file_handler.setLevel(log_level)
            
            # Create formatter
            formatter = logging.Formatter("%(asctime)s [%(levelname)8s] %(name)s: %(message)s")
            file_handler.setFormatter(formatter)
            
            # Add handler to root logger
            logging.getLogger('').addHandler(file_handler)
            
            log.info("Debug logging enabled to: %s" % log_file)
        except Exception as e:
            log.error("Failed to setup file logging: %s" % str(e))

def update_gdm_settings(name=None, value=None):
    gdm.clientDetails(settings.client_uuid, settings.player_name,
        settings.http_port, "Plex MPV Shim", "1.0")

def main():
    conf_file = conffile.get(APP_NAME,'conf.json')
    if os.path.isfile('settings.dat'):
        settings.migrate_config('settings.dat', conf_file)
    settings.load(conf_file)
    settings.add_listener(update_gdm_settings)
    
    # Setup file logging after settings are loaded
    setup_file_logging()
    
    if sys.platform.startswith("darwin"):
        try:
            multiprocessing.set_start_method('spawn')
        except RuntimeError:
            pass

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
    # daemon=True so the GDM registration UDP loop can't block process exit.
    gdm.start_all(daemon=True)

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
        # Wrap each cleanup so a single hung call can't prevent the rest from
        # running. We then force-exit at the end to bypass any lingering
        # non-daemon threads or subprocess pipes (notably python-mpv-jsonipc's
        # event handler under Python 3.14, which can't be join()ed at
        # finalization).
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
        # Force-exit. This skips Python finalization (which is what hangs on
        # Python 3.14 due to python-mpv-jsonipc's non-joinable thread) and
        # immediately reaps any remaining daemon threads / subprocess pipes.
        os._exit(0)

if __name__ == "__main__":
    main()
