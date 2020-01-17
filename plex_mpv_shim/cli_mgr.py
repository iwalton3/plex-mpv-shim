import time

class UserInterface(object):
    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop = lambda: None

    def run(self):
        while True:
            time.sleep(1)

userInterface = UserInterface()
