# sim/core/unit.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from .engine import s_to_us

@dataclass
class Buff:
    name: str
    expires_at_us: Optional[int] = None      # None = no timeout
    props: Dict[str, Any] = field(default_factory=dict)  # generic payload

@dataclass
class ChargeState:
    cur: int
    max: int
    recharge_s: float
    pending: list = field(default_factory=list)  # scheduled recharge events

class EmberPool:
    def __init__(self, maximum: int=500, starting: int=200):
        self.max = maximum
        self.cur = starting
        self.generated = 0
        self.spent = 0
    def gain(self, v: int):
        self.generated += v
        self.cur = min(self.max, self.cur + v)
    def spend(self, v: int) -> bool:
        if self.cur >= v:
            self.cur -= v
            self.spent += v
            return True
        return False

class SpiritPool:
    def __init__(self, owner: "Unit", maximum: float = 100, starting: float = 0):
        self.max = maximum
        self.cur = starting
        self.generated = 0.0
        self.spent = 0.0
        self.owner = owner
    def gain(self, v: int,bonus_spirit: float=0):
        v_scaled = (v*(self.owner.base_spirit_gain+bonus_spirit))
        self.generated =self.generated+ v_scaled
        self.cur = min(self.max, self.cur + v_scaled)
    def spend(self, v: int) -> bool:
        if self.cur >= v:
            self.cur -= v; self.spent += v; return True
        return False

class Unit:
    def __init__(self, name, eng, bus, rng: RNG, haste: float = 1.0, power: float = 100.0, base_crit: float = 0.05, base_spirit_gain: float = 1.0):
        self.name = name
        self.eng = eng
        self.bus = bus
        self.rng = rng
        self.haste = haste
        self.power = power
        self.base_crit = base_crit
        self.base_spirit_gain = base_spirit_gain

        self.ember = EmberPool(500,200)
        self.spiritbar = SpiritPool(self,100, 0)
        self.gcd_ready_us = 0
        self.busy_until_us = 0

        self.charges: Dict[str, ChargeState] = {}  # ability_id -> ChargeState

        # Debuffs/DoTs on this unit (e.g., Burn)
        self.auras: Dict[str, object] = {}
        # Self-buffs (e.g., Pyromania)
        self.buffs: Dict[str, Buff] = {}

        # Cooldowns (by ability id)
        self.cooldown_ready_us: Dict[str, int] = {}

        # accounting
        self.damage_by_ability: Dict[str, float] = {}
        self.cast_counts: Dict[str, int] = {}
        self.total_damage = 0.0

        self.active_dots: List[object] = []   # <-- track DotState instances


    def current_crit(self)->float:
        bonus=sum(float(b.props.get("crit_bonus",0.0)) for b in self.buffs.values())
        return max(0.0, min(1.0,self.base_crit+bonus))

    def dot_haste_multiplier(self) -> float:
        """Multiply caster haste for DoT tick rate by any buff-provided multipliers."""
        mult = 1.0
        for b in self.buffs.values():
            m = b.props.get("dot_haste_mult")
            if m is not None:
                mult *= float(m)
        return mult

    def dot_haste_bonus(self) -> float:
        """Add caster haste for DoT tick rate by any buff-provided multipliers."""
        add = 0.0
        for b in self.buffs.values():
            m = b.props.get("dot_haste_bonus")
            if m is not None:
                add += float(m)
        return add

    def haste_bonus(self) -> float:
        """Generic additive haste from buffs (e.g., +0.10 = +10%)."""
        return sum(float(b.props.get("haste_bonus", 0.0)) for b in self.buffs.values())

    def cast_haste_bonus(self) -> float:
        """Additive haste that applies specifically to CAST TIMES."""
        return sum(float(b.props.get("cast_haste_bonus", 0.0)) for b in self.buffs.values())

    # -------- damage/accounting --------
    def add_damage(self, amount: float, tag: str):
        self.total_damage += amount
        self.damage_by_ability[tag] = self.damage_by_ability.get(tag, 0.0) + amount

    # -------- auras on this unit --------
    def has_aura(self, name: str) -> bool:
        return name in self.auras

    def aura_remains_us(self, name: str, now_us: int) -> int:
        dot = self.auras.get(name)
        if not dot: return 0
        return max(0, getattr(dot, "expires_at_us", now_us) - now_us)

    def recalc_dot_timers(self):
        """Retimes all owned active DoTs to reflect a sudden change in tick interval."""
        now = self.eng.t_us
        for dot in list(self.active_dots):
            if getattr(dot, "owner", None) is self:
                try:
                    dot.retime(now)
                except Exception:
                    pass

    def add_buff(self, buff: Buff):
        self.buffs[buff.name] = buff

        # If this buff affects DoT haste, retime immediately
        if "dot_haste_mult" in buff.props:
            self.recalc_dot_timers()

        if buff.expires_at_us is not None:
            def expire():
                if self.buffs.get(buff.name) is buff and self.eng.t_us >= buff.expires_at_us:
                    self.buffs.pop(buff.name, None)
                    # On removal, also retime if it affected DoT haste
                    if "dot_haste_mult" in buff.props:
                        self.recalc_dot_timers()
            self.eng.schedule_at(buff.expires_at_us, expire)

    def has_buff(self, name: str) -> bool:
        return name in self.buffs

    def ensure_charges(self, ability_id: str, max_charges: int, recharge_s: float):
        st = self.charges.get(ability_id)
        if st is None:
            self.charges[ability_id] = ChargeState(cur=max_charges, max=max_charges, recharge_s=float(recharge_s))
        else:
            st.max = int(max_charges)
            st.recharge_s = float(recharge_s)
            st.cur = min(st.cur, st.max)

    def has_charge(self, ability_id: str) -> bool:
        st = self.charges.get(ability_id)
        return bool(st and st.cur > 0)

    def time_until_next_charge_us(self, ability_id: str) -> int:
        # optional helper if you later want smarter APL scheduling
        st = self.charges.get(ability_id)
        if not st or st.cur >= st.max or not st.pending:
            return 0
        # pending holds events scheduled at specific times; pull earliest
        next_evt = min(st.pending, key=lambda e: e.t_us)
        return max(0, next_evt.t_us - self.eng.t_us)

    def consume_charge(self, ability_id: str):
        st = self.charges[ability_id]
        if st.cur <= 0:
            return False
        st.cur -= 1

        # schedule a recharge only if we are below max
        if st.cur < st.max:
            def on_recharge(st_ref=st):
                # remove this evt from pending, then increment if still below max
                try:
                    st_ref.pending.remove(evt)
                except ValueError:
                    pass
                if st_ref.cur < st_ref.max:
                    st_ref.cur += 1

            evt = self.eng.schedule_in(s_to_us(st.recharge_s), on_recharge)
            st.pending.append(evt)
        return True


class TargetDummy(Unit):
    def __init__(self, eng, bus, rng):
        super().__init__("Target", eng, bus, rng, haste=1.0, power=0.0, base_crit=0.0)
        self.is_dead = False