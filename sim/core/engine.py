# sim/core/engine.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, List, Dict
import heapq, itertools

# Event phases for same-timestamp ordering
CAST_END, CHANNEL_TICK, DAMAGE, DOT_TICK, APL = range(5)

US = 1_000_000
def s_to_us(s: float) -> int: return int(round(s * US))
def us_to_s(us: int) -> float: return us / US

@dataclass(order=True)
class _Evt:
    t_us: int
    phase: int
    seq: int
    fn: Callable[[], None]
    cancelled: bool=False

class Engine:
    def __init__(self):
        self.t_us = 0
        self._q: List[_Evt] = []
        self._seq = itertools.count()

    def schedule_at(self, t_us: int, fn: Callable[[], None], phase: int=APL) -> _Evt:
        #phase_mod = float(phase/100000)
        evt = _Evt(t_us, phase, next(self._seq), fn, False) #
        heapq.heappush(self._q, evt)
        return evt

    def schedule_in(self, dt_us: int, fn: Callable[[], None], phase: int=APL) -> _Evt:
        return self.schedule_at(self.t_us + dt_us, fn, phase)

    def cancel(self, evt: _Evt) -> None:
        evt.cancelled = True

    def run_until(self, t_end_us: int, drain_same_time: bool=True) -> None:
        while self._q and self._q[0].t_us <= t_end_us:
            t = self._q[0].t_us
            self.t_us = t
            while self._q and self._q[0].t_us == self.t_us:
                evt = heapq.heappop(self._q)
                if evt.cancelled: continue
                evt.fn()

class Bus:
    def __init__(self): self._subs: Dict[str, list[Callable[..., None]]] = {}
    def sub(self, name: str, fn: Callable[..., None]): self._subs.setdefault(name, []).append(fn)
    def pub(self, name: str, **payload):
        for fn in tuple(self._subs.get(name, [])): fn(**payload)
