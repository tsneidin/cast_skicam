"""Randomized camera rotation without repeats within a catalog cycle."""
import random
import time


class ShuffledRotation:
    def __init__(self):
        self.catalog = None
        self.deck = []
        self.last_url = None

    def next(self, catalog, failed_until):
        if catalog is not self.catalog:
            self.catalog = catalog
            self.deck = []
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
        return camera, self.deck[-1] if self.deck else None
