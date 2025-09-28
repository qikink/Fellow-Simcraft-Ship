# sim/runtime/loader.py
from __future__ import annotations
from typing import Dict
import os, yaml
from ..core.engine import s_to_us, CAST_END, APL
from .components import AbilitySpec, Ctx, run_pipeline

def load_abilities_from_dir(path: str) -> Dict[str, AbilitySpec]:
    out: Dict[str, AbilitySpec] = {}
    for fn in os.listdir(path):
        if not fn.endswith(".yaml"): continue
        with open(os.path.join(path, fn), "r") as f:
            d = yaml.safe_load(f)
        spec = AbilitySpec(
            id=d["id"], name=d["name"],
            cast=d.get("cast", {"gcd_s":1.0, "cast_time_s":0.0}),
            cost=d.get("cost", {}),
            cooldown_s=float(d.get("cooldown_s", 0.0)),
            pipeline=d.get("pipeline", []),
            tags=d.get("tags", []),
            charges=d.get("charges"),
            off_gcd=bool(d.get("off_gcd", False)),
        )
        out[spec.id] = spec
    return out

def start_cast(ctx: Ctx) -> None:
    """Schedules cast end (or immediate), applies GCD/lockouts, then runs pipeline."""
    caster = ctx.caster
    eng = ctx.eng

    base_gcd_us  = s_to_us(float(ctx.spec.cast.get("gcd_s", 1.0))) if not ctx.spec.off_gcd else 0
    base_cast_us = s_to_us(float(ctx.spec.cast.get("cast_time_s", 0.0)))

    if base_cast_us > 0.0:
        eff_haste = max(1e-9, caster.haste + caster.haste_bonus() + caster.cast_haste_bonus())
        cast_us = base_cast_us / eff_haste
    else:
        cast_us = 0

    now = eng.t_us

    spirit_cost = int(ctx.spec.cost.get("spirit_bar", 0))
    if spirit_cost > 0 and not caster.spiritbar.spend(spirit_cost):
        return  # can't start

    # Apply gating
    caster.gcd_ready_us   = max(caster.gcd_ready_us,   now + base_gcd_us)
    caster.busy_until_us  = max(caster.busy_until_us,  now + cast_us)

    # Book-keep casts
    caster.cast_counts[ctx.spec.name] = caster.cast_counts.get(ctx.spec.name, 0) + 1



    # Cooldown bookkeeping: set when pressed (press-time CD model)
    if ctx.spec.charges and int(ctx.spec.charges.get("max", 0)) > 0:
        max_ch = int(ctx.spec.charges["max"])
        recharge_s = float(ctx.spec.charges.get("recharge_s", ctx.spec.cooldown_s or 0.0))
        caster.ensure_charges(ctx.spec.id, max_ch, recharge_s)
        caster.consume_charge(ctx.spec.id)
    elif ctx.spec.cooldown_s and ctx.spec.cooldown_s > 0:
        caster.cooldown_ready_us[ctx.spec.id] = now + s_to_us(ctx.spec.cooldown_s)

    # expose channel timing to components
    ctx.vars["cast_us"] = cast_us
    ctx.vars["cast_start_us"] = now

    ready_at = max(caster.gcd_ready_us, caster.busy_until_us)
    eng.schedule_at(ready_at, ctx.wake_apl, phase=APL)

    # At cast end, resolve pipeline and wake APL
    def on_cast_end():
        run_pipeline(ctx, ctx.spec.pipeline)
        ctx.wake_apl()  # <- this wake is what lets us weave off-GCD immediately after casts
    eng.schedule_at(now + cast_us, on_cast_end, phase=CAST_END)
