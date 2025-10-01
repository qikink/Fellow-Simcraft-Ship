# sim/core/rng.py
import random

class RNG:
    def __init__(self, seed: int = 1337):
        self.root = random.Random(seed)
        self._streams = {}

    def stream(self, name: str) -> random.Random:
        if name not in self._streams:
            self._streams[name] = random.Random(self.root.randint(0, 2**31 - 1))
        return self._streams[name]

    def roll(self, name: str, p: float) -> bool:
        p = max(0.0, min(1.0, p))
        return self.stream(name).random() < p

    def damage_variance(self,name: str):
        v = self.stream(name).random()*0.02
        v = v-.01
        return p