# sim/runners/target_dummy.py
from __future__ import annotations
from dataclasses import dataclass
from ..runtime.pack import load_character_spec, load_apl_factory, load_enabled_talents
from ..runtime.loader import load_abilities_from_dir, AbilitySpec, start_cast, Ctx
#from ..runtime.effects import load_effect_specs, EffectInstance
from typing import Dict
import os
from ..core.engine import Engine, Bus, s_to_us, APL
from ..core.unit import Unit, TargetDummy
from ..core.rng import RNG
from ..runtime.loader import load_abilities_from_dir, start_cast, AbilitySpec, Ctx
from ..core.apl import SimpleAPL

@dataclass
class SimConfig:
    duration_s: float = 300.0
    power: float = 100.0
    haste: float = 1.0
    base_crit: float = 0.05
    base_spirit_gain: float = 1.0
    talents: Dict[str, bool] = None          # e.g., {"bolt_vs_burn_20p": True}
    seed: int = 1337
    character: str = "Ardeos"



def run_sim(content_dir: str, cfg: SimConfig):
    eng, bus = Engine(), Bus()
    rng = RNG(cfg.seed)
    pack = load_character_spec(content_dir, cfg.character)
    make_apl = load_apl_factory(pack.paths["apl"])
    player = Unit("Player", eng, bus, rng, haste=cfg.haste, power=cfg.power, base_crit=cfg.base_crit,base_spirit_gain=cfg.base_spirit_gain)
    target = TargetDummy(eng, bus, rng)

    # Load abilities
    specs = load_abilities_from_dir(pack.paths["abilities"])
    #talent_dicts = load_enabled_talents(pack.paths["talents"], cfg.talents)
    # Helper: cooldown readiness
    def is_cd_ready(ability_id) -> bool:
        spec = specs[ability_id]
        # charges take precedence if present
        if spec.charges and int(spec.charges.get("max", 0)) > 0:
            max_ch = int(spec.charges["max"])
            recharge_s = float(spec.charges.get("recharge_s", spec.cooldown_s or 0.0))
            player.ensure_charges(spec.id, max_ch, recharge_s)
            return player.has_charge(spec.id)
            # classic cooldown path
        ready_at = player.cooldown_ready_us.get(spec.id, 0)
        return eng.t_us >= ready_at

    # APL
    def is_off_gcd(ability_id: str) -> bool:
        spec = specs.get(ability_id)
        return bool(spec and spec.off_gcd)

    def time_until_ready_us(ability_id: str) -> int:
        """Returns 0 if ready now; positive microseconds until ready; inf if unknown/not coming soon."""
        spec = specs.get(ability_id)
        if not spec:
            return inf
        now = eng.t_us

        # Charges path
        if spec.charges and int(spec.charges.get("max", 0)) > 0:
            max_ch = int(spec.charges["max"])
            recharge_s = float(spec.charges.get("recharge_s", spec.cooldown_s or 0.0))
            player.ensure_charges(spec.id, max_ch, recharge_s)
            st = player.charges.get(spec.id)
            if not st:
                return 0  # should not happen, but be permissive
            if st.cur > 0:
                return 0
            if st.pending:
                next_evt = min(st.pending, key=lambda e: e.t_us)
                return max(0, next_evt.t_us - now)
            # No pending event yet (e.g., never consumed) → treat as ready
            return 0

        # Classic cooldown path
        ready_at = player.cooldown_ready_us.get(spec.id, 0)
        return 0 if now >= ready_at else (ready_at - now)

    apl = make_apl(player, target, helpers={
        "is_cd_ready": is_cd_ready,
        "is_off_gcd": is_off_gcd,
        "time_until_ready_us": time_until_ready_us,
    })

    def wake_apl():
        now = eng.t_us

        # If casting, wake at cast end (CAST_END already does this), so just bail
        if now < player.busy_until_us:
            eng.schedule_at(player.busy_until_us, wake_apl, phase=APL)
            return

        # If GCD is still running: try an off-GCD weave now; otherwise, wait for ready
        if now < player.gcd_ready_us:
            choice = apl.choose_offgcd(now)
            if not choice:
                eng.schedule_at(player.gcd_ready_us, wake_apl, phase=APL)
                return
            spec = specs[choice]
            # Resource/readiness guard (redundant)
            if spec.cost.get("ember", 0) > player.ember.cur or not is_cd_ready(choice):
                eng.schedule_at(player.gcd_ready_us, wake_apl, phase=APL)
                return
            if spec.cost.get("spirit_bar", 0) > player.spiritbar.cur or not is_cd_ready(choice):
                eng.schedule_at(player.gcd_ready_us, wake_apl, phase=APL)
                return
            # Start off-GCD cast; its on_cast_end will call wake_apl() again
            ctx = Ctx(eng, bus, {"talents": cfg.talents or {}}, player, target, spec, wake_apl)


            start_cast(ctx)
            return

        # Gates are clear: pick an on-GCD action
        choice = apl.choose(now) # never stall
        spec = specs.get(choice)
        if not is_cd_ready(choice) or spec.cost.get("ember", 0) > player.ember.cur:
            # Should be rare; try again at the next "ready" moment
            ready_at = max(player.gcd_ready_us, player.busy_until_us)
            eng.schedule_at(ready_at, wake_apl, phase=APL)
            return

        ctx = Ctx(eng, bus, {"talents": cfg.talents or {}}, player, target, spec, wake_apl)
        start_cast(ctx)  # also schedules ready-at wake + cast-end wake

    # Kick off and run
    eng.schedule_at(0, wake_apl, phase=APL)
    eng.run_until(s_to_us(cfg.duration_s))

    # Report
    total = player.total_damage
    dps = total / cfg.duration_s
    by_ability = {k: (v, v/total*100 if total>0 else 0) for k,v in player.damage_by_ability.items()}
    return {
        "duration_s": cfg.duration_s,
        "total_damage": total,
        "dps": dps,
        "by_ability": by_ability,
        "casts": dict(player.cast_counts),
        "ember_generated": player.ember.generated,
        "ember_spent": player.ember.spent,
        "ember_end": player.ember.cur,
    }
