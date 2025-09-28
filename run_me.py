# run_me.py (at project root)
from sim.runners.target_dummy import run_sim, SimConfig
result = run_sim(content_dir="Content", cfg=SimConfig(
    duration_s=300.0, power=1.0, haste=1.15, base_crit=0.15,base_spirit_gain=1.15,
    talents={"bolt_vs_burn_20p": True, "boltcrit_on_burn_ppm": {"ppm": 2.0}},
    seed=1339,
    character="Ardeos",
))
print("DPS:", round(result["dps"], 2))
print("Casts:", result["casts"])
print("By ability:", {k: round(v[0],1) for k,v in result["by_ability"].items()})
