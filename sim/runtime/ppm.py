# sim/runtime/ppm.py
class PPMTracker:
    def __init__(self, ppm: float, rng, key: str):
        self.ppm = float(ppm)
        self.rng = rng
        self.key = key
        self.last_attempt_us: int | None = None

    def try_proc(self, now_us: int,haste_mult: float=1) -> bool:
        if self.ppm <= 0:
            self.last_attempt_us = now_us
            return False
        if self.last_attempt_us is None:
            self.last_attempt_us = now_us
            return False
        delta_s = max(0.0, (now_us - self.last_attempt_us) / 1_000_000.0)
        p = min(1.0, self.ppm * (delta_s / 60.0))*haste_mult
        self.last_attempt_us = now_us
        return self.rng.roll(self.key, p)
