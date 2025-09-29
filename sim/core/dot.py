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
    bonus_crit: float
    fixed_crit: float = -1
    preserve_phase_on_refresh: bool = False #assume you will "dot clip"
    refresh_overlap: float = 0.0 #assume no pandemic
    stacks: int = 0
    max_stacks: int = 0           # 0 = no stacking, >0 enables stacking
    stack_mult_per: float = 0.0   # e.g., 0.20 -> +20% per stack
    next_evt: Optional[object] = None
    expire_evt: Optional[object] = None

    def _remove_now(self):
        # idempotent removal
        if self.target.auras.get(self.name) is self:
            self.target.auras.pop(self.name, None)
        try:
            self.owner.active_dots.remove(self)
        except ValueError:
            pass
        if self.next_evt: self.next_evt.cancelled = True
        if self.expire_evt: self.expire_evt.cancelled = True
        self.next_evt = None
        self.expire_evt = None

    def schedule_expire(self):
        # cancel old, schedule new at current expires_at_us
        if self.expire_evt:
            self.expire_evt.cancelled = True
        eng = self.owner.eng
        def on_expire(dot=self):
            # remove only if still the same object and truly expired
            if dot.target.auras.get(dot.name) is dot and eng.t_us >= dot.expires_at_us:
                dot._remove_now()
                if(dot.name=="SearingBlaze"):
                    if dot.target.auras.get("SearingBlazeAmp"):
                        amp = dot.target.auras.get("SearingBlazeAmp")
                        amp["stacks"] = 0
                        dot.target.buffs.pop("SearingBlazeAmp",None)
        self.expire_evt = eng.schedule_at(self.expires_at_us, on_expire)

    def current_tick_interval_us(self) -> int:
        # Effective haste = base factor + additive bonuses.
        # Convention: owner.haste = 1.00 means +0% Haste; 1.25 means +25%.
        eff_haste = max(1e-9, (self.owner.haste if self.owner else 1.0) + self.owner.dot_haste_bonus())
        return max(1, int(round(self.base_tick_us / eff_haste)))

    def retime(self, now_us: int):
        # haste changed: recompute next tick time
        if self.next_evt: self.next_evt.cancelled = True
        I = self.current_tick_interval_us()
        phase0 = self.anchor_us + self.first_delay_us
        k = max(0, (now_us - phase0 + I - 1) // I) + 1
        next_tick = max(now_us, phase0 + k * I)
        if next_tick < self.expires_at_us:
            self.next_evt = self.owner.eng.schedule_at(next_tick, self._tick_cb, phase=DOT_TICK)
        else:
            # let expire event handle cleanup
            self.next_evt = None

    def schedule_first_tick(self):
        eng = self.owner.eng
        self.schedule_expire()  # <-- ensure there is always an up-to-date expire event
        t = max(eng.t_us, self.anchor_us + self.first_delay_us)
        self.next_evt = eng.schedule_at(t, self._tick_cb, phase=DOT_TICK)

    def _tick_cb(self):
        eng = self.owner.eng
        if eng.t_us >= self.expires_at_us or getattr(self.target, "is_dead", False):
            self._remove_now()
            return

        if eng.t_us >= self.expires_at_us or getattr(self.target, "is_dead", False):
            self.next_evt = None
            return
        #publish the pre-event to listeners who may modify it
        self.owner.bus.pub("dot_pre_tick", dot=self, t_us=eng.t_us)

        mult = 1.0
        is_crit = False

        temp_bonus_crit = 0
        if getattr(self, "_force_crit_tick", False):
            temp_bonus_crit = 1

        if hasattr(self, "_force_crit_tick"):
            delattr(self, "_force_crit_tick")


        if self.fixed_crit>=0:
            if self.fixed_crit + self.bonus_crit + temp_bonus_crit > 1:  # grievous crits
                mult *= (self.fixed_crit + self.bonus_crit + temp_bonus_crit)

            if self.owner.rng.roll("dot_crit", self.fixed_crit + self.bonus_crit + temp_bonus_crit): #if dot has a fixed crit value, use that instead of character crit
                mult *= 2.0
                is_crit = True
        else:
            if self.owner.current_crit() + self.bonus_crit + temp_bonus_crit > 1:  # grievous crits
                mult *= (self.owner.current_crit() + self.bonus_crit + temp_bonus_crit)

            if self.owner.rng.roll("dot_crit", self.owner.current_crit() + self.bonus_crit + temp_bonus_crit):
                mult *= 2.0
                is_crit = True

        # deal damage
        mult *= (1.0 + (self.stacks * self.stack_mult_per if self.max_stacks > 0 else 0.0))

        if self.name == "SearingBlaze":
            amp = self.target.auras.get("SearingBlazeAmp")
            if amp:
                mult *= (1.0 + amp.get("stacks", 0) * amp.get("per", 0.0))

        dmg = self.coeff_per_tick * self.owner.power * mult

        self.owner.add_damage(dmg, self.name)

        self.owner.bus.pub("dot_tick", dot=self, t_us=eng.t_us,crit=is_crit)
        # gain resources
        self.owner.spiritbar.gain(dmg / 600)
        if self.ember_per_tick:
            self.owner.ember.gain(self.ember_per_tick)
        if self.spirit_per_tick:
            self.owner.spiritbar.gain(self.spirit_per_tick)


        # schedule next anchored tick honoring haste
        I = self.current_tick_interval_us()
        phase0 = self.anchor_us + self.first_delay_us
        k = max(0, (eng.t_us - phase0 + I - 1) // I) + 1
        next_tick = phase0 + k * I
        # if next tick would land after expiry, let expire event do the cleanup
        #if next_tick >= self.expires_at_us:
            #vestigal, left for posterity
            #print("I'm dying!")
            #self.next_evt = None
            #return
        self.next_evt = eng.schedule_at(next_tick, self._tick_cb, phase=DOT_TICK)

    def refresh(self, now_us: int, new_base_duration_us: int):
        self.base_duration_us = new_base_duration_us
        self.expires_at_us = now_us + new_base_duration_us
        if not self.preserve_phase_on_refresh:
            self.anchor_us = now_us
            self.first_delay_us = self.current_tick_interval_us()
        self.schedule_expire()  # <-- reschedule

        # schedule next anchored tick honoring haste
        #I = self.current_tick_interval_us()
        #phase0 = self.anchor_us + self.first_delay_us
        #k = max(0, (eng.t_us - phase0 + I - 1) // I) + 1
        #next_tick = phase0 + k * I

        #self.next_evt = eng.schedule_at(next_tick, self._tick_cb, phase=DOT_TICK)

    def add_stacks(self, now_us: int, add: int, new_duration_us: Optional[int] = None):
        if self.max_stacks > 0:
            self.stacks = min(self.max_stacks, self.stacks + add)
        if new_duration_us is not None:
            self.expires_at_us = now_us + new_duration_us
            self.schedule_expire()  # <-- reschedule
