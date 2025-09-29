# sim/tools/harness.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Tuple, Any
import math
import json
import hashlib

from sim.runners.target_dummy import run_sim, SimConfig

# ---------- Inputs ----------
@dataclass
class Attrs:
    name: str                 # character/pack id (e.g., "pyro")
    haste: float              # e.g., 1.15 means +15% haste; or whatever your engine expects
    base_crit:  float              # base crit as fraction, e.g., 0.15 for 15%
    base_spirit_gain: float   # e.g., 1.00 (no bonus), 1.20 (+20%)
    power: float

@dataclass
class BatchRequest:
    content_dir: str
    attrs: Attrs
    talent_sets: List[Dict[str, Any]]           # e.g., [{"1A": True, "1C": True}, {...}, ...]
    schedules: List[List[Tuple[float, int]]]    # e.g., [[(0,1),(15,3),(30,1)], [(0,5)], ...]
    run_count: int = 100
    duration_s: float = 300.0                   # 5 minutes default
    base_seed: int = 1337                       # change for a different Monte Carlo repeat

# ---------- Helpers ----------
def _format_talents(tal: Dict[str, Any]) -> str:
    # Compact, stable key for the table
    # If values have params, keep them (sorted json)
    keys = sorted(tal.keys())
    if all(v is True or v == {} for v in tal.values()):
        return "+".join(keys) if keys else "(none)"
    # parametric talents: stable JSON by sorting keys
    return json.dumps({k: tal[k] for k in keys}, sort_keys=True)

def _format_schedule(enc: List[Tuple[float, int]]) -> str:
    # e.g., "0:1 → 15:3 → 30:1"
    return " → ".join(f"{t}:{n}" for (t, n) in enc)

def _extract_dps(result: Any, duration_s: float) -> float:
    # Be flexible about return shape
    if isinstance(result, dict):
        if "dps" in result:
            return float(result["dps"])
        if "total_damage" in result:
            return float(result["total_damage"]) / float(duration_s)
    if hasattr(result, "dps"):
        return float(result.dps)
    if hasattr(result, "total_damage"):
        return float(result.total_damage) / float(duration_s)
    raise ValueError("run_sim result did not contain dps or total_damage")

def _seed_for(base_seed: int, talents: Dict[str, Any], schedule: List[Tuple[float,int]], i: int) -> int:
    # Stable per (talents, schedule, replicate index) seed
    h = hashlib.blake2b(digest_size=8)
    h.update(str(base_seed).encode())
    h.update(json.dumps(talents, sort_keys=True).encode())
    h.update(json.dumps(schedule).encode())
    h.update(str(i).encode())
    return int.from_bytes(h.digest(), "big") % (2**31 - 1)

# ---------- Core ----------
def run_batch(req: BatchRequest):
    """
    Returns: list of rows dicts with keys: 'talents', 'schedule', 'avg_dps'
    You can easily convert to pandas.DataFrame if you like.
    """
    rows = []
    # Build a stats override payload the runner (or your pack) can read
    # power=1.0, haste=1.05, base_crit=.05,base_spirit_gain=1.05,
    stats = {
        "haste": req.attrs.haste,
        "base_crit": req.attrs.base_crit,
        "base_spirit_gain": req.attrs.base_spirit_gain,
        "power": req.attrs.power
    }

    for tal in req.talent_sets:
        tal_key = _format_talents(tal)
        for enc in req.schedules:
            sched_key = _format_schedule(enc)

            total = 0.0
            for i in range(req.run_count):
                print("Run: ",i)
                seed = _seed_for(req.base_seed, tal, enc, i)
                # Build SimConfig for this replicate
                cfg = SimConfig(
                    duration_s=req.duration_s,
                    seed=seed,
                    talents=tal,
                    character=req.attrs.name,
                    encounter=enc,
                    power=stats["power"],
                    haste=stats["haste"],
                    base_crit=stats["base_crit"],
                    base_spirit_gain=stats["base_spirit_gain"]
                )
                try:
                    setattr(cfg, "stats", stats)  # harmless if SimConfig already declares it
                except Exception:
                    pass

                result = run_sim(req.content_dir, cfg)
                dps = _extract_dps(result, req.duration_s)
                total += dps

            avg = total / float(req.run_count)
            rows.append({
                "talents": tal_key,
                "schedule": sched_key,
                "average_dps": round(avg, 4),
            })

    return rows

# ---------- Optional: pretty print ----------
def print_table(rows: List[dict]):
    # simple fixed-width display; swap for pandas if you prefer
    if not rows:
        print("(no results)")
        return
    w1 = max(len(r["talents"]) for r in rows + [{"talents":"talents"}])
    w2 = max(len(r["schedule"]) for r in rows + [{"schedule":"schedule"}])
    print(f"{'talents'.ljust(w1)} | {'schedule'.ljust(w2)} | average_dps")
    print("-" * (w1 + w2 + 15 + 3))
    for r in rows:
        print(f"{r['talents'].ljust(w1)} | {r['schedule'].ljust(w2)} | {r['average_dps']:.4f}")

# ---------- CLI example ----------
if __name__ == "__main__":
    # Example usage; adjust paths and values to your repo
    req = BatchRequest(
        content_dir="content",  # root of your character packs
        attrs=Attrs(name="Ardeos", power=1.0, haste=1.05, base_crit=.05,base_spirit_gain=1.05),
        talent_sets=[
            {"1A": True},
            {"1B": True},
            {"1C": True},
            {"2A": True},
            {"2B": True},
            {"2C": True},
            {"3A": True},
            {"3B": True},
            {"3C": True},
            {"4A": True},
            {"4B": True},
            {"4C": True},
            {"5A": True},
            {"5B": True},
            {"5C": True},
            {"6A": True},
            {"6B": True},
            {"6C": True},
        ],
        schedules=[
            [(0, 1)],                            # pure ST
            [(0, 3)],          # cleave
        ],
        run_count=10,
        duration_s=300.0,
        base_seed=1337,
    )
    rows = run_batch(req)
    print_table(rows)
