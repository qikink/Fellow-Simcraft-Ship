# sim/core/world.py
from __future__ import annotations
from typing import List, Tuple
from .engine import s_to_us
from .unit import TargetDummy

class World:
    def __init__(self, eng, bus, rng):
        self.eng, self.bus, self.rng = eng, bus, rng
        self.enemies: List[TargetDummy] = []
        self._seq = 0
        self.sample_names = ["AA","BB","CC","DD","EE","FF","GG","HH"]

    # ---- queries ----
    def enemies_alive(self) -> List[TargetDummy]:
        return [u for u in self.enemies if not getattr(u, "is_dead", False)]

    def primary(self):
        for u in self.enemies:
            if not u.is_dead:
                return u
        return None

    # ---- mutations ----
    def spawn_one(self):
        self._seq += 1
        u = TargetDummy(self.eng, self.bus, self.rng)
        u.name = f"Target#{self._seq}"
        self.enemies.append(u)
        self.bus.pub("enemy_spawn", unit=u, t_us=self.eng.t_us)
        return u

    def despawn_one(self, u: TargetDummy):
        if getattr(u, "is_dead", False):
            return
        u.is_dead = True
        # Optional: proactively clear auras to stop further ticks
        u.auras.clear()
        self.bus.pub("enemy_despawn", unit=u, t_us=self.eng.t_us)

    # bring alive count to exactly n
    def set_enemy_count(self, n: int):
        alive = self.enemies_alive()
        while len(alive) < n:
            self.spawn_one()
            alive = self.enemies_alive()
        while len(alive) > n:
            self.despawn_one(alive.pop())

def schedule_encounter(world: World, plan: list[tuple[float, int]]):
    """plan = [(t_s, count), ...] — at each t_s set alive enemies to count."""
    eng = world.eng
    for t_s, cnt in sorted(plan, key=lambda x: x[0]):
        def cb(count=cnt):
            world.set_enemy_count(int(count))
        eng.schedule_at(s_to_us(float(t_s)), cb)
    # If first change isn’t at t=0, start with its initial count or 1
    if not plan or plan[0][0] > 0:
        world.set_enemy_count(plan[0][1] if plan else 1)
