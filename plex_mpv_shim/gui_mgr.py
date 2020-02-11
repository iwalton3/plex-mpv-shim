from pystray import Icon, MenuItem, Menu
from PIL import Image
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
from multiprocessing import Process, Queue
import threading
import sys
import logging
import queue
import os.path

APP_NAME = "plex-mpv-shim"
from .conffile import confdir

if (sys.platform.startswith("win32") or sys.platform.startswith("cygwin")) and getattr(sys, 'frozen', False):
    # Detect if bundled via pyinstaller.
    # From: https://stackoverflow.com/questions/404744/
    icon_file = os.path.join(sys._MEIPASS, "systray.png")
else:
    icon_file = os.path.join(os.path.dirname(__file__), "systray.png")
log = logging.getLogger('gui_mgr')

# From https://stackoverflow.com/questions/6631299/
# This is for opening the config directory.
def _show_file_darwin(path):
    subprocess.Popen(["open", path])

def _show_file_linux(path):
    subprocess.Popen(["xdg-open", path])

def _show_file_win32(path):
    subprocess.Popen(["explorer", path])

_show_file_func = {'darwin': _show_file_darwin, 
                   'linux': _show_file_linux,
                   'win32': _show_file_win32,
                   'cygwin': _show_file_win32}

try:
    show_file = _show_file_func[sys.platform]
    def open_config():
        show_file(confdir(APP_NAME))
except KeyError:
    open_config = None
    log.warning("Platform does not support opening folders.")

# Setup a log handler for log items.
log_cache = deque([], 1000)
root_logger = logging.getLogger('')

class GUILogHandler(logging.Handler):
    def __init__(self):
        self.callback = None
        super().__init__()

    def emit(self, record):
        log_entry = self.format(record)
        log_cache.append(log_entry)

        if self.callback:
            try:
                self.callback(log_entry)
            except Exception:
                pass

guiHandler = GUILogHandler()
guiHandler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)8s] %(message)s"))
root_logger.addHandler(guiHandler)

# Why am I using another process for the GUI windows?
# Because both pystray and tkinter must run
# in the main thread of their respective process.

class LoggerWindow(threading.Thread):
    def __init__(self):
        self.dead = False
        threading.Thread.__init__(self)

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = LoggerWindowProcess(self.queue, self.r_queue)
    
        def handle(message):
            self.handle("append", message)
        
        self.process.start()
        handle("\n".join(log_cache))
        guiHandler.callback = handle
        while True:
            action, param = self.r_queue.get()
            if action == "die":
                self._die()
                break
    
    def handle(self, action, params=None):
        self.queue.put((action, params))

    def stop(self, is_source=False):
        self.r_queue.put(("die", None))
    
    def _die(self):
        guiHandler.callback = None
        self.handle("die")
        self.process.terminate()
        self.dead = True

class LoggerWindowProcess(Process):
    def __init__(self, queue, r_queue):
        self.queue = queue
        self.r_queue = r_queue
        Process.__init__(self)

    def update(self):
        try:
            self.text.config(state=tk.NORMAL)
            while True:
                action, param = self.queue.get_nowait()
                if action == "append":
                    self.text.config(state=tk.NORMAL)
                    self.text.insert(tk.END, "\n")
                    self.text.insert(tk.END, param)
                    self.text.config(state=tk.DISABLED)
                    self.text.see(tk.END)
                elif action == "die":
                    self.root.destroy()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.text.after(100, self.update)

    def run(self):
        root = tk.Tk()
        self.root = root
        root.title("Application Log")
        text = tk.Text(root)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand = tk.YES)
        text.config(wrap=tk.WORD)
        self.text = text
        yscroll = tk.Scrollbar(command=text.yview)
        text['yscrollcommand'] = yscroll.set
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.config(state=tk.DISABLED)
        self.update()
        root.mainloop()
        self.r_queue.put(("die", None))

# Q: OK. So you put Tkinter in it's own process.
#    Now why is Pystray in another process too?!
# A: Because if I don't, MPV and GNOME Appindicator
#    try to access the same resources and cause the
#    entire application to segfault.
#
# I suppose this means I can put the Tkinter GUI back
# into the main process. This is true, but then the
# two need to be merged, which is non-trivial.

class UserInterface:
    def __init__(self):
        self.dead = False
        self.open_player_menu = lambda: None
        self.icon_stop = lambda: None
        self.log_window = None
        self.preferences_window = None

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = STrayProcess(self.queue, self.r_queue)
        self.process.start()

        while True:
            try:
                action, param = self.r_queue.get()
                if hasattr(self, action):
                    getattr(self, action)()
                elif action == "die":
                    self._die()
                    break
            except KeyboardInterrupt:
                log.info("Stopping due to CTRL+C.")
                self._die()
                break
    
    def handle(self, action, params=None):
        self.queue.put((action, params))

    def stop(self):
        self.handle("die")
    
    def _die(self):
        self.process.terminate()
        self.dead = True

        if self.log_window and not self.log_window.dead:
            self.log_window.stop()

    def login_servers(self):
        is_logged_in = clientManager.try_connect()
        if not is_logged_in:
            self.show_preferences()

    def show_console(self):
        if self.log_window is None or self.log_window.dead:
            self.log_window = LoggerWindow()
            self.log_window.start()
    
    def open_config_brs(self):
        if open_config:
            open_config()
        else:
            log.error("Config opening is not available.")

class STrayProcess(Process):
    def __init__(self, queue, r_queue):
        self.queue = queue
        self.r_queue = r_queue
        Process.__init__(self)

    def run(self):
        def get_wrapper(command):
            def wrapper():
                self.r_queue.put((command, None))
            return wrapper

        def die():
            self.icon_stop()

        menu_items = [
            MenuItem("Show Console", get_wrapper("show_console")),
            MenuItem("Application Menu", get_wrapper("open_player_menu")),
            MenuItem("Open Config Folder", get_wrapper("open_config_brs")),
            MenuItem("Quit", die)
        ]

        icon = Icon(APP_NAME, menu=Menu(*menu_items))
        icon.icon = Image.open(icon_file)
        self.icon_stop = icon.stop
        icon.run()
        self.r_queue.put(("die", None))

userInterface = UserInterface()
