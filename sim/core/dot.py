# sim/core/dot.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from .engine import DOT_TICK

@dataclass
class DotState:
    name: str
    owner: "Unit"
    target: "Unit"
    anchor_us: int
    first_delay_us: int
    base_duration_us: int
    expires_at_us: int
    base_tick_us: int
    coeff_per_tick: float
    ember_per_tick: float
    spirit_per_tick: float
    preserve_phase_on_refresh: bool = False
    stacks: int = 0
    max_stacks: int = 0           # 0 = no stacking, >0 enables stacking
    stack_mult_per: float = 0.0   # e.g., 0.20 -> +20% per stack
    next_evt: Optional[object] = None

    def current_tick_interval_us(self) -> int:
        # Effective haste = base factor + additive bonuses.
        # Convention: owner.haste = 1.00 means +0% Haste; 1.25 means +25%.
        eff_haste = max(1e-9, (self.owner.haste if self.owner else 1.0) + self.owner.dot_haste_bonus())
        return max(1, int(round(self.base_tick_us / eff_haste)))

    def retime(self, now_us: int):
        if self.next_evt:
            self.next_evt.cancelled = True;
            self.next_evt = None
        I = self.current_tick_interval_us()
        phase0 = self.anchor_us + self.first_delay_us
        k = max(0, (now_us - phase0 + I - 1) // I) + 1
        next_tick = max(now_us, phase0 + k * I)
        if next_tick < self.expires_at_us:
            self.next_evt = self.owner.eng.schedule_at(next_tick, self._tick_cb, phase=DOT_TICK)

    def schedule_first_tick(self):
        eng = self.owner.eng
        t = max(eng.t_us, self.anchor_us + self.first_delay_us)
        self.next_evt = eng.schedule_at(t, self._tick_cb, phase=DOT_TICK)

    def _tick_cb(self):
        eng = self.owner.eng
        if eng.t_us >= self.expires_at_us or getattr(self.target, "is_dead", False):
            self.next_evt = None
            return
        #publish the event to listeners
        self.owner.bus.pub("dot_tick", dot=self, t_us=eng.t_us)

        # deal damage
        mult = 1.0 + (self.stacks * self.stack_mult_per if self.max_stacks > 0 else 0.0)
        dmg = self.coeff_per_tick * self.owner.power * mult
        if self.owner.rng.roll("dot_crit", self.owner.current_crit()):
            dmg *= 2.0
        self.owner.add_damage(dmg, self.name)

        # gain resources
        if self.ember_per_tick:
            print(self.ember_per_tick)
            self.owner.ember.gain(self.ember_per_tick)
        if self.spirit_per_tick:
            self.owner.spiritbar.gain(self.spirit_per_tick)

        # schedule next (anchored, no drift)
        I = self.current_tick_interval_us()
        phase0 = self.anchor_us + self.first_delay_us
        k = max(0, (eng.t_us - phase0 + I - 1) // I) + 1
        next_tick = phase0 + k * I
        if next_tick >= self.expires_at_us:
            self.next_evt = None
            return
        self.next_evt = eng.schedule_at(next_tick, self._tick_cb, phase=DOT_TICK)

    def refresh(self, now_us: int, new_base_duration_us: int):
        self.base_duration_us = new_base_duration_us
        self.expires_at_us = now_us + new_base_duration_us
        if not self.preserve_phase_on_refresh:
            self.anchor_us = now_us
            self.first_delay_us = self.current_tick_interval_us()

    def add_stacks(self, now_us: int, add: int, new_duration_us: Optional[int] = None):
        if self.max_stacks > 0:
            self.stacks = min(self.max_stacks, self.stacks + add)
        if new_duration_us is not None:
            self.expires_at_us = now_us + new_duration_us
