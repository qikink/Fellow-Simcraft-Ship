# sim/runners/target_dummy.py
from __future__ import annotations
from dataclasses import dataclass
from ..runtime.pack import load_character_spec, load_apl_factory, load_enabled_talents
from ..runtime.talents import apply_talent_patches, attach_talent_listeners
from ..runtime.loader import load_abilities_from_dir, AbilitySpec, start_cast, Ctx
#from ..runtime.effects import load_effect_specs, EffectInstance
from typing import Dict
import os
from ..core.engine import Engine, Bus, s_to_us, APL
from ..core.unit import Unit, TargetDummy
from ..core.rng import RNG
from ..core.world import World, schedule_encounter
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
    encounter: list[tuple[float,int]] | None = None   # NEW, e.g. [(0,1),(15,3),(30,1)]



def run_sim(content_dir: str, cfg: SimConfig):
    eng, bus = Engine(), Bus()
    rng = RNG(cfg.seed)
    pack = load_character_spec(content_dir, cfg.character)
    make_apl = load_apl_factory(pack.paths["apl"])

    world = World(eng, bus, rng)
    schedule_encounter(world, cfg.encounter or [(0, 1)])  # default: 1 target full sim

    player = Unit("Player", eng, bus, rng, haste=cfg.haste, power=cfg.power, base_crit=cfg.base_crit,base_spirit_gain=cfg.base_spirit_gain)
    target = TargetDummy(eng, bus, rng)
    print(cfg.talents)
    ctx_cfg = {
        "talents": cfg.talents or {},
        "resource_aliases": pack.resource_aliases,
        "world": world,  # <-- add this
    }


    # Load abilities
    specs = load_abilities_from_dir(pack.paths["abilities"])
    talent_dicts = load_enabled_talents(pack.paths["talents"], cfg.talents)
    apply_talent_patches(specs, talent_dicts)
    _ = attach_talent_listeners(talent_dicts, player, bus)

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

    def enemies_alive():
        return world.enemies_alive()

    def count_enemies() -> int:
        return len(world.enemies_alive())

    def count_aura(aura_name: str, owner_only: bool = True) -> int:
        c = 0
        for u in world.enemies_alive():
            dot = u.auras.get(aura_name)
            if not dot:
                continue
            if owner_only and dot.owner is not player:
                continue
            c += 1
        return c

    def next_enemy_missing_aura(aura_name: str):
        for u in world.enemies_alive():
            dot = u.auras.get(aura_name)
            if not dot or dot.owner is not player:
                return u
        return world.primary()  # fallback

    apl = make_apl(player, target, world, helpers={
        "is_cd_ready": is_cd_ready,
        "is_off_gcd": is_off_gcd,
        "time_until_ready_us": time_until_ready_us,
        "count_enemies": count_enemies,
        "count_aura": count_aura,
        "next_enemy_missing_aura": next_enemy_missing_aura,
        "enemies_alive": enemies_alive,
    })

    def _split_choice(choice):
        if isinstance(choice, tuple) and len(choice) == 2:
            return choice[0], choice[1]
        return choice, world.primary()

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

            ctx = Ctx(eng, bus, ctx_cfg, player, target, spec, wake_apl)


            start_cast(ctx)
            return

        # Gates are clear: pick an on-GCD action

        choice, target_for_cast = apl.choose(now) # consult the APL
        #choice, target_for_cast = _split_choice(pre_choice) #pick a target
        spec = specs.get(choice)
        if not is_cd_ready(choice) or spec.cost.get("ember", 0) > player.ember.cur:
            # Should be rare; try again at the next "ready" moment
            ready_at = max(player.gcd_ready_us, player.busy_until_us)
            eng.schedule_at(ready_at, wake_apl, phase=APL)
            return

        ctx = Ctx(eng, bus, ctx_cfg, player, target_for_cast or world.primary(), spec, wake_apl)
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
