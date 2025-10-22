# sim/runtime/talents.py
from __future__ import annotations

from pkgutil import extend_path
from typing import Dict, List, Any, Callable
from ..core.engine import s_to_us
from typing import Dict, List, Any, Iterable, Tuple
from ..core.unit import reduce_cooldown_us, grant_charge, Unit, Buff
from ..core.dot import DotState
from .ppm import PPMTracker
from .components import run_pipeline, Ctx
from ..core.world import World
import copy

INJECT_TAG = "__injected_by_talent__"

def _iter_steps_recursive(steps: List[dict], path: Tuple=()) -> Iterable[Tuple[dict, Tuple]]:
    """
    Yields (step, path) for every step in the pipeline, including nested lists
    under ANY key whose value looks like a list of step dicts (has 'type').
    Path is a tuple of segments describing where the step lives (for debugging).
    """
    for i, step in enumerate(steps or []):
        # yield current step
        yield step, path + (("pipeline", i),)
        # recurse into any child list that looks like a pipeline
        for k, v in step.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "type" in v[0]:
                yield from _iter_steps_recursive(v, path + ((f"{step.get('type','?')}.{k}",),))



def _iter_steps_with_parent(steps, path=()):
    """Yield (parent_list, index, step, path) for every step, recursively."""
    for i, step in enumerate(steps or []):
        yield steps, i, step, path + (("pipeline", i),)
        for k, v in step.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "type" in v[0]:
                yield from _iter_steps_with_parent(v, path + ((f"{step.get('type','?')}.{k}",),))


def _find_matches_prepatch(spec_pipeline, where, *, talent_id: str):
    """Return a stable list of matches taken BEFORE any insertions."""
    want_type = where.get("type")
    want_name = where.get("name")
    matches = []
    for parent, idx, step, path in _iter_steps_with_parent(spec_pipeline):
        # ignore steps we injected earlier for this same talent
        if step.get(INJECT_TAG) == talent_id:
            continue
        if want_type and step.get("type") != want_type:
            continue
        if want_name is not None and step.get("name") != want_name:
            continue
        matches.append((parent, idx, step, path))
    return matches

def _insert_steps(parent, idx, new_step):
    parent.insert(idx, new_step)


def _apply_insert_op(ab, patch, talent_id, *, before: bool, warn_no_match=True):
    where = patch["where"]
    step_template = patch["step"]
    matches = _find_matches_prepatch(ab.pipeline, where, talent_id=talent_id)

    if not matches:
        if warn_no_match:
            print(f"[talents] warn: no matches for {('insert_before' if before else 'insert_after')} "
                  f"in '{ab.id}' where={where} (talent {talent_id})")
        return

    # Insert from the BACK to keep earlier indices valid
    for parent, idx, step, path in reversed(matches):
        new_step = copy.deepcopy(step_template)
        #print("new_step:", new_step)
        if isinstance(new_step, list):
            new_step = new_step[0]
        new_step[INJECT_TAG] = talent_id
        insert_idx = idx if before else idx + 1
        _insert_steps(parent, insert_idx, new_step)

#Character Mods
def apply_talent_stat_mods(character: Unit, talents: List[dict]) -> None:
    for t in talents: #do behavior changing talents
        type = t.get("type")
        if(type != "stat_mod"):
            continue
        for s in t.get("stats", []):
            mod = s.get("mod")
            amount = s.get("amount")
            stat_name = s.get("stat")
            if stat_name=="critical_strike_multiplier":
                if mod == "add":
                    character.critical_strike_multiplier += amount
                elif mod == "multiply":
                    character.critical_strike_multiplier *= amount

# ---------- Ability patching (load-time) ----------
def apply_talent_patches(specs: Dict[str, Any], talents: List[dict]) -> None:
    """
    Mutates 'specs' in-place based on 'patches' declared by enabled talents.
    Patch format (list items):
      - ability: <ability_id>
        where: { type: "damage" | "dot" | ..., name: "<dot_name_optional>" }
        op: "scale" | "add" | "set"
        field: "coeff" | "coeff_per_tick" | "duration_s" | ... (numeric fields)
        by: <number>      # for scale/add
        to: <number>      # for set
        index: <int>      # optional: if multiple steps match, pick 0-based index
    """
    warn_no_match = True
    for t in talents: #do behavior changing talents
        #print(t)
        tid = t.get("id", "?")
        for p in (t.get("patches") or []):
            ab_field = p["ability"]
            if isinstance(ab_field, list):
                ability_ids = ab_field
            elif ab_field == "*":
                ability_ids = list(specs.keys())
            else:
                ability_ids = [ab_field]

            for ab_id in ability_ids:
                ab = specs.get(ab_id)
                if not ab:
                    if warn_no_match:
                        print(f"[talents] warn: ability '{ab_id}' not found (talent {tid})")
                    continue

                meta = getattr(ab, "meta", None)
                if meta is None:
                    ab.meta = meta = {}
                done = meta.setdefault("patched_by", set())
                op = p.get("op")
                if op == "insert_after":
                    _apply_insert_op(ab, p, tid, before=False, warn_no_match=warn_no_match)
                elif op == "insert_before":
                    _apply_insert_op(ab, p, tid, before=True, warn_no_match=warn_no_match)

    for t in talents: #do simple modification talent# s
        tid = t.get("id", "?")
        for p in (t.get("patches") or []):
            ab_id = p["ability"]
            ab = specs.get(ab_id)

            if not ab:
                if warn_no_match:
                    print(f"[talents] warn: ability '{ab_id}' not found for patch in talent {t.get('id')}")
                continue

            where = p.get("where", {})
            want_type = where.get("type")
            want_name = where.get("name")  # optional (e.g., dot name)
            matches: List[Tuple[dict, Tuple]] = []
            for step, path in _iter_steps_recursive(ab.pipeline):
                if want_type and step.get("type") != want_type:
                    continue
                if want_name is not None and step.get("name") != want_name:
                    continue
                matches.append((step, path))
            if not matches:
                if warn_no_match:
                    print(
                        f"[talents] warn: no steps matched where={where} in ability '{ab_id}' (talent {t.get('id')})")
                continue

            targets = matches
            if "index" in p:
                idx = int(p["index"])
                if 0 <= idx < len(matches):
                    targets = [matches[idx]]
                else:
                    if warn_no_match:
                        print(
                            f"[talents] warn: index {idx} out of range ({len(matches)}) for ability '{ab_id}' (talent {t.get('id')})")
                    continue

            op = p["op"];
            if "field" in p:
                field = p["field"]
                for (step, path) in targets:
                    if op == "set":
                        step[field] = float(p["to"])
                    else:
                        if field not in step:
                            # silently skip if field missing for add/scale; feel free to warn instead
                            if warn_no_match:
                                print(
                                    f"[talents] warn: field '{field}' missing at {path} in '{ab_id}' (talent {t.get('id')})")
                            continue
                        if op == "add":
                            step[field] = float(step[field]) + float(p["by"])
                        elif op == "scale":
                            step[field] = float(step[field]) * float(p["by"])
                        else:
                            if warn_no_match:
                                print(f"[talents] warn: unknown op '{op}' in talent {t.get('id')}")


# ---------- Event-driven listeners (runtime) ----------
def attach_talent_listeners(specs: Dict[str, Any], world: World, talents: List[dict], player, bus) -> List[Callable[[], None]]:
    """
    Subscribes to bus events for talents that declare type: on_dot_tick_extend.
    Returns a list of callables to detach (no-op if your bus lacks unsubscribe).
    Talent shape:
      type: on_dot_tick_extend
      source_dot: "FireballDoT"
      extend:
        - { dot: "EngulfingFlames", seconds: 0.5 }
        - { dot: "SearingBlaze",   seconds: 0.5 }
      owner_only: true  # default
    """
    detachers = []

    for t in talents:
        if t.get("type") != "on_dot_tick_extend":
            continue
        src = t["source_dot"]
        ext_list = t.get("extend", [])
        owner_only = bool(t.get("owner_only", True))
        def handler(dot=None, t_us=None, **_):
            # fire only when YOUR source dot ticks
            if dot is None or dot.name != src or dot.owner is not player:
                return
            eng = player.eng
            target = dot.target
            for spec in ext_list:
                name = spec["dot"]
                extra_s = float(spec.get("seconds", 0.0))
                d = target.auras.get(name)
                if not d:
                    continue
                if owner_only and d.owner is not player:
                    continue
                # extend expiry safely
                d.expires_at_us += s_to_us(extra_s)

                # schedule/refresh an expire check at the new time
                def expire_check(dt=d, tgt=target):
                    # remove only if still the same object and actually expired
                    if tgt.auras.get(dt.name) is dt and eng.t_us >= dt.expires_at_us:
                        tgt.auras.pop(dt.name, None)
                        try:
                            player.active_dots.remove(dt)
                        except ValueError:
                            pass
                eng.schedule_at(d.expires_at_us, expire_check)

        bus.sub("dot_tick", handler)
        detachers.append(lambda: None)  # fill if you add unsubscribe later

    for t in talents:
        if t.get("type") != "on_dot_tick_cd":
            continue
        src = t["source_dot"]
        red_list = t.get("reduce", [])
        owner_only = bool(t.get("owner_only", True))
        def handler(dot=None, t_us=None, crit=False,**_):
            # fire only when YOUR source dot ticks
            is_crit = crit
            if dot is None or dot.name != src or dot.owner is not player:
                return
            eng = player.eng
            target = dot.target
            for spec in red_list:
                extra_s = float(spec.get("seconds", 0.0))
                extra_s_crit = float(spec.get("seconds_crit", extra_s))
                delta_us = s_to_us(extra_s_crit if is_crit else extra_s)
                cd = spec["cd"]
                if not cd:
                    continue
                # reduce cooldown safely
                reduce_cooldown_us(player, player.eng, cd, delta_us)


        bus.sub("dot_tick", handler)
        detachers.append(lambda: None)  # fill if you add unsubscribe later


    for t in talents:
        if t.get("type") != "on_dot_crit_apply_dot":
            continue

        sources = t.get("sources", ["*"])  # list of dot names or ["*"]
        exclude = set(t.get("exclude", []))  # avoid recursion: e.g., ["CinderEcho"]
        #owner_only = bool(t.get("owner_only", True))
        apply_cfg = t["apply"]  # dict describing the proc dot

        # apply_cfg supports either:
        #  - fixed coeff: { name, duration_s, tick_s, coeff_per_tick, first_tick? }
        #  - proportional: { name, duration_s, tick_s, percent_of_tick, use_crit_amount: true|false, first_tick? }

        src_any = (sources == ["*"])
        pct = apply_cfg.get("percent_of_tick")
        use_crit_amount = bool(apply_cfg.get("use_crit_amount", True))

        def handler(dot=None, t_us=None, crit=False, amount=None, **_):
            if dot is None or not crit:
                return
            if (not src_any) and (dot.name not in sources):
                return
            dst_name = apply_cfg["name"]
            if dot.name == dst_name and (dst_name in exclude or t.get("exclude_self", True)):
                return  # prevent self-proc chains

            chance = apply_cfg["chance"] #check to see if it procced
            if not player.rng.roll("proc", chance):
                return
            # compute coeff for the proc DoT
            if pct is not None:
                # scale from this tick's damage
                base = float(amount) if use_crit_amount else float(dot.coeff_per_tick * dot.owner.power)
                total = pct * base
                coeff_per_tick = None
                total_damage = total * (apply_cfg.get("total_scale", 1.0))
            else:
                total_damage = None
                coeff_per_tick = float(apply_cfg["coeff_per_tick"])

            dur_us = s_to_us(apply_cfg["duration_s"])
            base_tick_us = s_to_us(apply_cfg["tick_s"])
            first_tick = apply_cfg["first_tick"]
            now = player.eng.t_us
            eff_haste = max(1e-9, player.haste + player.dot_haste_bonus())
            eff_tick_s = base_tick_us / eff_haste
            first_delay_us = 0 if first_tick == "immediate" else int(round(base_tick_us / eff_haste))

            new_dot = DotState(
                name=dst_name, owner=player, target=dot.target,
                anchor_us=now, first_delay_us=first_delay_us,
                base_duration_us=dur_us, expires_at_us=now + dur_us,
                base_tick_us=base_tick_us, coeff_per_tick=coeff_per_tick,
                ember_per_tick=0, preserve_phase_on_refresh=True,
                spirit_per_tick=0, bonus_crit=0
            )
            # tag for analytics if you want
            #new_dot.src_ability_id = ctx.vars.get("last_hit_ability", ctx.spec.id)

            # register & schedule
            dot.target.auras[dst_name] = new_dot
            player.active_dots.append(new_dot)
            new_dot.schedule_first_tick()

        bus.sub("dot_tick", handler)
        detachers.append(lambda: None)


    for t in talents:
        if t.get("type") != "on_cast_ppm_proc":
            continue

        ability_source = t.get("source_cast")            # "detonate"
        ppm = float(t.get("ppm", 1.0))
        effects = t.get("effects", [])       # list of dicts
        tracker = PPMTracker(ppm, player.rng, key=f"ppm:{t.get('id','?')}")

        def on_cast_end(ability_id=None, t_us=None, caster=None, **_):
            if caster is not player or (ability_source is not None and ability_id != ability_source):
                return
            if not tracker.try_proc(t_us,player.haste):
                return

            # proc! apply effects
            for eff in effects:
                et = eff.get("type")
                if et == "grant_charge":
                    grant_charge(player, player.eng, eff["ability"], int(eff.get("amount", 1)))
                elif et == "guarantee_next_crit":
                    player.grant_next_crit(eff["ability"], int(eff.get("charges", 1)))
                elif et == "apply_buff":
                    name = eff.get("name")
                    expires_at_us = s_to_us(eff.get("duration_s"))+t_us
                    props={}
                    props["affected_cast"] = eff.get("affected_cast")
                    props["affected_crit_bonus"] = eff.get("affected_crit_bonus")
                    props["affected_haste_bonus"] = eff.get("affected_haste_bonus")
                    buff = Buff(name=name,expires_at_us=expires_at_us, props=props)

                    player.add_buff(buff)

        bus.sub("cast_end", on_cast_end)
        detachers.append(lambda: None)

    for t in talents:
        if t.get("type") != "stack_amp_on_damage":
            continue

        source = t.get("source", {})  # e.g., {"dot_name": "AgonizingBlaze"} or {"ability": "agonizing_blaze"}
        per_stack = float(t.get("per_stack", 0.03))  # 3%
        max_stacks = int(t.get("max_stacks", 10))
        aura_name = t.get("aura", "SearingBlazeAmp")
        owner_only = bool(t.get("owner_only", True))

        def _bump(target, now_us, owner):
            # store stacks on the TARGET as a light “aura” dict
            a = target.auras.get(aura_name)
            if not a:
                a = target.auras[aura_name] = {"stacks": 0, "per": per_stack, "max": max_stacks, "owner": owner}
            if owner_only and a.get("owner") is not owner:
                # if some other player's aura exists, either ignore or replace; we’ll ignore
                return
            a["stacks"] = min(a["max"], a["stacks"] + 1)

        # react to DoT ticks
        def on_tick(dot=None, t_us=None, **_):
            if dot is None: return
            if owner_only and dot.owner is not player: return
            # match either by dot name or by the ability that applied it
            wants_name = source.get("dot_name")
            wants_ability = source.get("ability")
            if wants_name and dot.name != wants_name: return
            if wants_ability and getattr(dot, "src_ability_id", None) != wants_ability: return
            _bump(dot.target, t_us, dot.owner)


        bus.sub("dot_tick", on_tick)

    for t in talents:
        if t.get("type") != "on_dot_pre_tick_force_crit":
            continue

        dots = set(t.get("dots", []))  # e.g., {"SearingBlaze","EngulfingFlames"}
        owner_only = bool(t.get("owner_only", True))
        base_chance = float(t.get("base_chance", 0.04))  # 4%
        scale_factor = float(t.get("base_crit_scale", 0.20))  # +20% of base crit
        def on_pre_tick(dot=None, t_us=None, **_):
            if dot is None:
                return
            if dots and dot.name not in dots:
                return
            if owner_only and dot.owner is not player:
                return
            p = base_chance + scale_factor * float(getattr(player, "base_crit", 0.0))
            p = max(0.0, min(1.0, p))
            if player.rng.roll(f"precrit:{t.get('id', '?')}", p):
                setattr(dot, "_force_crit_tick", True)

        bus.sub("dot_pre_tick", on_pre_tick)
        detachers.append(lambda: None)


    for t in talents:
        if t.get("type") != "modify_cast":
            continue

        ability_source = t.get("source_cast",None)            # "detonate"
        modify_list = t.get("source_casts", [])
        buff_name = t["buff_name"]
        required_stacks = t.get("required_stacks") or 0
        effects = t.get("effects") or []
        def on_cast_start(ability_id=None, t_us=None, caster=None, ctx=None,**_):
            if caster is not player or (ability_id != ability_source and ability_id not in modify_list):
                return
            if not player.buffs.get(buff_name) or not "stacks" in player.buffs[buff_name].props or player.buffs[buff_name].props["stacks"] < required_stacks:
                return
            # proc! apply effects
            for eff in effects:
                et = eff.get("type")
                if et == "make_cast_instant":
                    if required_stacks > 0 and not eff.get("waterfall",False):
                        player.buffs[buff_name].props["stacks"] = player.buffs[buff_name].props["stacks"] - required_stacks
                    if player.buffs[buff_name].props["stacks"] == 0:
                        player.remove_buff(player.buffs.get(buff_name))
                    ctx.spec.cast["modified_cast_time_s"]=0
                if et == "reduce_cast_time":
                    if required_stacks > 0 and not eff.get("waterfall",False):
                        player.buffs[buff_name].props["stacks"] = player.buffs[buff_name].props["stacks"] - required_stacks
                    if  player.buffs[buff_name].props["stacks"] == 0:
                        player.remove_buff(player.buffs.get(buff_name))
                    ctx.spec.cast["modified_cast_time_s"] = min(0,ctx.spec.cast["cast_time_s"]-eff.get("amount",0))
                if et == "grant_crit_chance":
                    if required_stacks > 0 and not eff.get("waterfall",False):
                        player.buffs[buff_name].props["stacks"] = player.buffs[buff_name].props["stacks"] - required_stacks
                    if  player.buffs[buff_name].props["stacks"] == 0:
                        player.remove_buff(player.buffs.get(buff_name))
                    bonus = eff.get("amount",0)
                    caster.grant_next_crit_bonus(ability_id,1,bonus)

                # extend here with other effect types as needed

        bus.sub("cast_start", on_cast_start)
        detachers.append(lambda: None)

    for t in talents:
        if t.get("type") != "on_cast_proc":
            continue

        ability_source = t["source_cast"]            # "detonate"
        effects_b = t.get("effects") or []
        def on_cast_start(ability_id=None, t_us=None, caster=None, ctx=None,**_):
            if caster is not player or ability_id != ability_source:
                return
            for eff in effects_b:
                if player.rng.roll("cast proc",eff.get("chance")):
                    et = eff.get("type")
                    if et == "damage_mult":
                        mult_from_ctx = float(ctx.vars.pop("damage_mult", 1.0))
                        mult_from_ctx *= eff.get("amount",1.0)
                        ctx.vars["damage_mult"] = mult_from_ctx
                        #print("multiplying damage by:", mult_from_ctx)
                    if not eff.get("waterfall"):
                        return #end if we're at a terminal (non-waterfall) step

                # extend here with other effect types as needed

        bus.sub("cast_start", on_cast_start)
        detachers.append(lambda: None)

    for t in talents:
        if t.get("type") != "apply_debuff":
            continue

        buff_name = t["buff_name"]            # "detonate"
        effects = t.get("effects") or []
        def on_debuff_expire(t_us=None, buff=None, target=None, ctx=None,**_):
            if buff.name != buff_name:
                return
            for eff in effects:
                #print("effect:",eff)
                et = eff.get("type")
                if et == "run_pipeline":
                    pipe = eff.get("pipeline")
                    attributed_ability = eff.get("ability")
                    eng=player.eng
                    cfg={'world':world}
                    spec=specs.get(attributed_ability,None)
                    expire_ctx = Ctx( eng=eng, bus=bus, cfg=cfg, caster=player, target=target, spec=spec, wake_apl=None)
                    #print("trying to run with ctx:",expire_ctx)
                    run_pipeline(expire_ctx, pipe)
                if not eff.get("waterfall"):
                    return #end if we're at a terminal (non-waterfall) step

                # extend here with other effect types as needed

        bus.sub("buff_expire", on_debuff_expire)
        detachers.append(lambda: None)

    for t in talents:
        if t.get("type") != "on_spend_mod":
            continue

        affected = t.get("affected") or []
        def on_spend_ember(t_us=None,**_):
            for aff in affected:
                et = aff.get("type")
                if et == "reduce_cd":
                    delta_us = s_to_us(aff.get("amount",0))
                    cd = aff.get("ability")
                    if not cd:
                        continue
                    #print("reducing cooldown of: ",cd," by: ",delta_us)
                    reduce_cooldown_us(player, player.eng, cd, delta_us)

                # extend here with other effect types as needed

        bus.sub("spend_ember", on_spend_ember)
        detachers.append(lambda: None)

    for t in talents:
        if t.get("type") == "on_generate_mod":
            effects_a = t.get("effects", [])
            required_buff = None
            if t.get("require_buff"):
                required_buff = t.get("buff")
            def on_generate_ember(t_us,amount:int=0, **_):
                if not player.rng.roll("on generate proc", t.get("chance", 1)):
                    return
                if required_buff is not None and not player.has_buff(required_buff):
                    return
                for eff in effects_a:
                    et = eff.get("type")
                    if et == "guarantee_next_crit":
                        ability = eff.get("ability")
                        player.grant_next_crit(ability, int(eff.get("charges", 1)))
                    elif et == "apply_buff":
                        name = eff.get("name")
                        expires_at_us = s_to_us(eff.get("duration_s"))+t_us
                        props={}
                        props["affected_cast"] = eff.get("affected_cast")
                        props["affected_crit_bonus"] = eff.get("affected_crit_bonus")
                        props["affected_haste_bonus"] = eff.get("affected_haste_bonus")
                        buff = Buff(name=name,expires_at_us=expires_at_us, props=props)
                        player.add_buff(buff)
                    elif et == "run_pipeline":
                        pipe = eff.get("pipeline")
                        attributed_ability = eff.get("ability")
                        eng=player.eng
                        cfg={'world':world}
                        target=world.primary
                        spec=specs.get(attributed_ability,None)
                        generate_ctx = Ctx( eng=eng, bus=bus, cfg=cfg, caster=player, target=target, spec=spec, wake_apl=None)
                        run_pipeline(generate_ctx, pipe)

            bus.sub("generate_ember", on_generate_ember)
            detachers.append(lambda: None)

        if t.get("type") == "on_hit_mod":
            effects = t.get("effects", [])  # list of dicts
            source_ability = t.get("source_ability")
            def on_hit(t_us,ability_id,outer_step_type: str = 'default',**_):
                if ability_id != source_ability:
                    return
                if outer_step_type == 'fanout' and not t.get("on_fanout"):
                    return
                for eff in effects:
                    if eff.get("type") == 'extend_buff':
                        extension = s_to_us(eff.get("amount_s"))
                        buff = eff.get("buff")
                        player.extend_buff(buff, extension)
            bus.sub("damage_done", on_hit)
            detachers.append(lambda: None)

    return detachers


