# sim/runtime/talents.py
from __future__ import annotations
from typing import Dict, List, Any, Callable
from ..core.engine import s_to_us
from typing import Dict, List, Any, Iterable, Tuple


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
    for t in talents:
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
                    print(f"[talents] warn: no steps matched where={where} in ability '{ab_id}' (talent {t.get('id')})")
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
            field = p["field"]
            for (step, path) in targets:
                print(step,path)
                if op == "set":
                    step[field] = float(p["to"])
                else:
                    if field not in step:
                        # silently skip if field missing for add/scale; feel free to warn instead
                        if warn_no_match:
                            print(
                                f"[talents] warn: field '{field}' missing at {path} in '{ab_id}' (talent {t.get('id')})")
                        continue
                    if op == "scale":
                        print("scale")
                        print(step[field],p["by"])
                        step[field] = float(step[field]) * float(p["by"])
                    elif op == "add":
                        print("add")
                        print(step[field], p["by"])
                        step[field] = float(step[field]) + float(p["by"])
                    else:
                        if warn_no_match:
                            print(f"[talents] warn: unknown op '{op}' in talent {t.get('id')}")

# ---------- Event-driven listeners (runtime) ----------
def attach_talent_listeners(talents: List[dict], player, bus) -> List[Callable[[], None]]:
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
        print(t)
        src = t["source_dot"]
        ext_list = t.get("extend", [])
        owner_only = bool(t.get("owner_only", True))
        print(src,ext_list)
        def handler(dot=None, t_us=None, **_):
            # fire only when YOUR source dot ticks
            if dot is None or dot.name != src or dot.owner is not player:
                return
            print("handling!")
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

    return detachers
