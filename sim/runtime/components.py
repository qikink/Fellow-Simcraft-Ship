# sim/runtime/components.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Dict, Any, List, Optional, Union
from ..core.engine import s_to_us, CAST_END, DAMAGE, APL
from ..core.dot import DotState
from ..core.unit import Buff

ComponentExec = Callable[['Ctx', Dict[str, Any]], None]
COMPONENTS: Dict[str, ComponentExec] = {}

def component(name: str):
    def reg(fn: ComponentExec):
        COMPONENTS[name] = fn; return fn
    return reg

@dataclass
class AbilitySpec:
    id: str
    name: str
    cast: dict                  # {gcd_s, cast_time_s}
    cost: dict                  # {ember}
    cooldown_s: float
    pipeline: List[dict]
    tags: List[str]
    charges: Optional[dict] = None  # e.g., {"max": 2, "recharge_s": 15.0}
    off_gcd: bool = False

class Ctx:
    """Context passed through pipeline and casts."""
    def __init__(self, eng, bus, cfg, caster, target, spec: AbilitySpec, wake_apl: Callable[[], None]):
        self.eng, self.bus, self.cfg = eng, bus, cfg
        self.caster, self.target, self.spec = caster, target, spec
        self.vars: Dict[str, Any] = {}
        self.wake_apl = wake_apl

    @property
    def power(self) -> float: return self.caster.power
    def crit_chance(self) -> float: return self.caster.current_crit()

    def expr(self, s: Union[str, float, int]) -> float:
        if not isinstance(s, str): return float(s)
        env = {
            "power": self.caster.power,
            "haste": self.caster.haste,
            "ember": self.caster.ember.cur,
            "vars": self.vars,
        }
        return float(eval(s, {"__builtins__": {}}, env))

def run_pipeline(ctx: Ctx, pipeline: List[dict]) -> None:
    i = 0
    while i < len(pipeline):
        step = pipeline[i]
        fn = COMPONENTS[step["type"]]
        fn(ctx, step)
        i += 1

# ---------------- Components ----------------

@component("damage")
def comp_damage(ctx: Ctx, step: dict):
    coeff = ctx.expr(step["coeff"])
    mult = 1.0
    # optional multiplicative mods (e.g., Bolt vs Burn)
    for mod in step.get("mods", []):
        if mod.get("type") == "mult_if_target_has_aura":
            if not mod.get("config_flag") or ctx.cfg["talents"].get(mod["config_flag"], False):
                if ctx.target.has_aura(mod["aura"]):
                    mult *= float(mod.get("mult", 1.0))

    base = coeff * ctx.power * mult
    # dynamic crit roll
    is_crit = ctx.caster.rng.roll("crit", ctx.crit_chance())
    dmg = base * (2.0 if is_crit else 1.0)
    ctx.caster.spiritbar.gain(dmg/400) #gain spirit for damage dealth, approx 1% per 400% of primary stat dealt
    ctx.caster.add_damage(dmg, ctx.spec.name)

@component("resource_gain")
def comp_resource_gain(ctx: Ctx, step: dict):
    if step.get("pool") == "ember":
        ctx.caster.ember.gain(float(step.get("amount", 0)))
    if step.get("pool") == "spiritbar":
        ctx.caster.spiritbar.gain(float(step.get("amount", 0)))

@component("resource_spend")
def comp_resource_spend(ctx: Ctx, step: dict):
    if step.get("pool") == "ember":
        ctx.caster.ember.spend(float(step.get("amount", 0)))
    if step.get("pool") == "spiritbar":
        ctx.caster.spiritbar.spend(float(step.get("amount", 0)))

@component("stack_dot")   # create-or-add-stacks then (re)start ticking if needed
def comp_stack_dot(ctx: Ctx, step: dict):
    name = step["name"]
    dur_us = s_to_us(float(step["duration_s"]))
    base_tick_us = s_to_us(float(step.get("tick_s", 1.0)))
    coeff_per_tick = float(step.get("coeff_per_tick", 0.0))
    stack_mult_per = float(step.get("stack_mult_per", 0.0))
    max_stacks = int(step.get("max_stacks", 0))
    add_stacks = int(step.get("add_stacks", 1))
    first = step.get("first_tick", "interval")
    first_delay_us = int(round(base_tick_us / max(1e-9, ctx.caster.haste + ctx.caster.dot_haste_bonus()))) if first == "interval" else 0

    dot = ctx.target.auras.get(name)
    now = ctx.eng.t_us
    if dot is None:
        dot = DotState(
            name=name, owner=ctx.caster, target=ctx.target,
            anchor_us=now, first_delay_us=first_delay_us,
            base_duration_us=dur_us, expires_at_us=now + dur_us,
            base_tick_us=base_tick_us, coeff_per_tick=coeff_per_tick,
            ember_per_tick=0, spirit_per_tick=0,preserve_phase_on_refresh=True,
            stacks=0, max_stacks=max_stacks, stack_mult_per=stack_mult_per
        )
        ctx.target.auras[name] = dot
        ctx.caster.active_dots.append(dot)
        dot.add_stacks(now, add_stacks, new_duration_us=dur_us)
        dot.schedule_first_tick()
        def on_expire():
            if ctx.target.auras.get(name) is dot and ctx.eng.t_us >= dot.expires_at_us:
                ctx.target.auras.pop(name, None)
                if dot in ctx.caster.active_dots: ctx.caster.active_dots.remove(dot)
        ctx.eng.schedule_at(dot.expires_at_us, on_expire)
    else:
        dot.add_stacks(now, add_stacks, new_duration_us=dur_us)

@component("channel")
def comp_channel(ctx: Ctx, step: dict):
    """
    Schedule repeated 'on_tick' actions evenly across the (hasted) cast/channel duration.
    Requires 'ticks: int' and 'on_tick: [components...]' in the step.
    Assumes start_cast() set ctx.vars['cast_us'] and ctx.vars['cast_start_us'].
    """
    ticks = int(step["ticks"])
    on_tick = step.get("on_tick", [])
    cast_us = int(ctx.vars.get("cast_us", 0))
    start_us = int(ctx.vars.get("cast_start_us", ctx.eng.t_us))
    if ticks <= 0 or cast_us <= 0: return
    spacing = cast_us // ticks  # integer microseconds; last tick may land before cast end

    def make_cb(i: int):
        def _cb():
            # run the on_tick pipeline in-place
            run_pipeline(ctx, on_tick)
        return _cb

    for i in range(1, ticks + 1):
        t = start_us + i * spacing
        ctx.eng.schedule_at(t, make_cb(i), phase=DAMAGE)

@component("dot")
def comp_dot(ctx: Ctx, step: dict):
    name = step["name"]
    dur_us = s_to_us(float(step["duration_s"]))
    base_tick_us = s_to_us(float(step["tick_s"]))
    coeff_per_tick = float(step.get("coeff_per_tick", 0.0))
    ember_per_tick = int(step.get("ember_per_tick", 0))
    spirit_per_tick = int(step.get("spirit_per_tick", 0))
    first = step.get("first_tick", "interval")  # "interval" or 0
    first_delay_us = int(round(base_tick_us / max(1e-9, ctx.caster.haste))) if first == "interval" else 0

    dot = ctx.target.auras.get(name)
    now = ctx.eng.t_us
    if dot is None:
        dot = DotState(
            name=name, owner=ctx.caster, target=ctx.target,
            anchor_us=now, first_delay_us=first_delay_us,
            base_duration_us=dur_us, expires_at_us=now + dur_us,
            base_tick_us=base_tick_us, coeff_per_tick=coeff_per_tick,
            ember_per_tick=ember_per_tick, spirit_per_tick=spirit_per_tick,
            preserve_phase_on_refresh=False
        )
        ctx.target.auras[name] = dot
        ctx.caster.active_dots.append(dot)        # <-- track ownership
        dot.schedule_first_tick()

        def on_expire():
            if ctx.target.auras.get(name) is dot and ctx.eng.t_us >= dot.expires_at_us:
                ctx.target.auras.pop(name, None)
                # remove from owner's active list
                if dot in ctx.caster.active_dots:
                    ctx.caster.active_dots.remove(dot)
        ctx.eng.schedule_at(dot.expires_at_us, on_expire)
    else:
        dot.refresh(now, dur_us)

@component("apply_buff")
def comp_apply_buff(ctx: Ctx, step: dict):
    name = step["name"]
    dur = step.get("duration_s")
    expires = ctx.eng.t_us + s_to_us(float(dur)) if dur is not None else None
    known = {"type","name","duration_s"}
    props = {k:v for k,v in step.items() if k not in known}
    ctx.caster.add_buff(Buff(name=name, expires_at_us=expires, props=props))

# sim/runtime/components.py
def _world(ctx):
    return (ctx.cfg or {}).get("world", None)

@component("fanout")
def comp_fanout(ctx: Ctx, step: dict):
    """
    Select N targets and run 'pipeline' for each target.
    step:
      select:
        count: int                     # how many to hit (try to get this many)
        include_primary: bool = True   # prioritize current primary first
        owner: "enemies" | "allies" = "enemies"
        prefer_missing_aura: str | None
        owner_only_for_aura: bool = True
        distinct: bool = True          # don't hit same target twice
      pipeline: [ ... ]                # components to run per target
    """
    world = _world(ctx)
    assert world is not None, "fanout requires world in ctx.cfg"

    sel = step.get("select", {})
    want = int(sel.get("count", 1))
    include_primary = bool(sel.get("include_primary", True))
    prefer_aura = sel.get("prefer_missing_aura")
    owner_only_for_aura = bool(sel.get("owner_only_for_aura", True))
    distinct = bool(sel.get("distinct", True))
    side = sel.get("owner", "enemies")

    # candidate pool
    if side == "enemies":
        pool = world.enemies_alive()
    else:
        # Add allies() helper later if needed; for now stick to enemies.
        pool = world.enemies_alive()

    if not pool:
        return

    # Build priority lists
    primary = world.primary() if include_primary else None
    chosen = []
    def add(u):
        if not u: return
        if distinct and u in chosen: return
        chosen.append(u)

    if primary:
        add(primary)

    if prefer_aura:
        missing = []
        haveit = []
        for u in pool:
            dot = u.auras.get(prefer_aura)
            ok = False
            if not dot:
                ok = True
            elif not owner_only_for_aura:
                ok = False  # it's present (by anyone)
            else:
                ok = (dot.owner is not ctx.caster)  # treat as "missing *yours*"
            (missing if ok else haveit).append(u)
        for u in missing: add(u)
        for u in haveit: add(u)
    else:
        for u in pool: add(u)

    targets = chosen[:want] if distinct else (chosen * want)[:want]
    if not targets:
        return

    # Run the inner pipeline once per target
    for t in targets:
        prev = ctx.target
        try:
            ctx.target = t
            run_pipeline(ctx, step.get("pipeline", []))
        finally:
            ctx.target = prev


