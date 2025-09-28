# sim/runtime/pack.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Dict, Any, Optional
import os, importlib.util, yaml
import glob

@dataclass
class CharacterSpec:
    id: str
    name: str
    base_stats: Dict[str, float]          # power, haste, base_crit, etc.
    resource_aliases: Dict[str, str]      # eg {"mana": "spirit"} (optional)
    gcd_s: float                          # default GCD
    paths: Dict[str, str]                 # {"abilities": ..., "talents": ..., "apl": ...}

def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f)

def load_character_spec(content_root: str, char_id: str) -> CharacterSpec:
    root = os.path.join(content_root, char_id)
    cfg = _load_yaml(os.path.join(root, "character.yaml"))
    return CharacterSpec(
        id=char_id,
        name=cfg.get("name", char_id.title()),
        base_stats=cfg.get("base_stats", {"power":100.0,"haste":1.0,"base_crit":0.05}),
        resource_aliases=cfg.get("resource_aliases", {}),
        gcd_s=float(cfg.get("gcd_s", 1.0)),
        paths={
            "root": root,
            "abilities": os.path.join(root, "abilities"),
            "talents": os.path.join(root, "talents"),
            "apl": os.path.join(root, "apl.py"),
        },
    )

def load_apl_factory(apl_path: str) -> Callable[..., Any]:
    spec = importlib.util.spec_from_file_location("char_apl", apl_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader, f"Cannot load APL at {apl_path}"
    spec.loader.exec_module(mod)  # type: ignore
    assert hasattr(mod, "make_apl"), "apl.py must define make_apl(player, target, helpers) -> APL"
    return mod.make_apl

def load_enabled_talents(talents_dir: str, enabled: Optional[dict]) -> list[dict]:
    enabled = enabled or {}
    dicts = []
    for p in glob.glob(os.path.join(talents_dir, "*.yaml")):
        d = _load_yaml(p)
        if d["id"] in enabled:
            ov = enabled[d["id"]]
            if isinstance(ov, dict):
                d["rate"] = {**d.get("rate", {}), **ov}
            dicts.append(d)
    return dicts
