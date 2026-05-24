import logging
import threading

from .player import playerManager

class ActionThread(threading.Thread):
    def __init__(self):
        self.trigger        = threading.Event()
        self.halt           = False

        threading.Thread.__init__(self)
        # Daemonize so process exit isn't blocked if the loop is stuck on a
        # @synchronous('_lock') call inside playerManager.update().
        self.daemon = True

    def stop(self):
        self.halt = True
        self.trigger.set()
        # Bounded join so shutdown can't hang here indefinitely.
        self.join(timeout=3)

    def run(self):
        force_next = False
        while not self.halt:
            if playerManager.is_active() or force_next:
                playerManager.update()

            force_next = False
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()

actionThread = ActionThread()
