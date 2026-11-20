"""Randomized camera rotation without repeats within a catalog cycle."""
import random
import time


class ShuffledRotation:
    def __init__(self):
        self.catalog = None
        self.deck = []
        self.last_url = None
        self.history = []
        self.history_index = -1

    def next(self, catalog, failed_until):
        if catalog is not self.catalog:
            self.catalog = catalog
            self.deck = []
            self.history = []
            self.history_index = -1

        if 0 <= self.history_index < len(self.history) - 1:
            self.history_index += 1
            camera = self.history[self.history_index]
            self.last_url = camera["url"]
            next_cam = (
                self.history[self.history_index + 1]
                if self.history_index + 1 < len(self.history)
                else (self.deck[-1] if self.deck else None)
            )
            return camera, next_cam

        if not self.deck:
            now = time.time()
            self.deck = [cam for cam in catalog if failed_until.get(cam["url"], 0) <= now]
            random.shuffle(self.deck)
            if len(self.deck) > 1 and self.deck[-1]["url"] == self.last_url:
                self.deck[0], self.deck[-1] = self.deck[-1], self.deck[0]
        if not self.deck:
            return None, None
        camera = self.deck.pop()
        self.last_url = camera["url"]
        self.history.append(camera)
        if len(self.history) > 200:
            self.history.pop(0)
        self.history_index = len(self.history) - 1
        return camera, self.deck[-1] if self.deck else None

    def prev(self):
        if not self.history:
            return None, None
        if self.history_index > 0:
            self.history_index -= 1
        camera = self.history[self.history_index]
        self.last_url = camera["url"]
        next_cam = (
            self.history[self.history_index + 1]
            if self.history_index + 1 < len(self.history)
            else (self.deck[-1] if self.deck else None)
        )
        return camera, next_cam

