# run_me.py (at project root)
from sim.runners.target_dummy import run_sim, SimConfig
result = run_sim(content_dir="Content", cfg=SimConfig(
    duration_s=300.0, power=1.0, haste=1.1, base_crit=.4,base_spirit_gain=1.05,
    talents={"1A": True, "1B": True, "2C": True, "5C": True},
    seed=1339,
    character="Rime",
    encounter=[(0,1)],
    movement=.15,
))
print("DPS:", round(result["dps"], 2))
print("Casts:", result["casts"])
print("By ability:", {k: round(v[0],1) for k,v in result["by_ability"].items()})
