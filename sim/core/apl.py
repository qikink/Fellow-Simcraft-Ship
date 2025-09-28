# sim/core/apl.py
from __future__ import annotations
from .engine import s_to_us, us_to_s,APL

class SimpleAPL:
    """
    Priority:
    """

    def __init__(self, player, target, is_cd_ready, is_off_gcd,time_until_ready_us, *, debug: str = "all", logger=None, bus=None):
        """
        debug: "off" | "unique" | "all"
          - "unique": log only when the chosen action differs from the previous decision (default)
          - "all": log every time a choice is made
          - "off": no logging
        logger: callable(str) -> None (defaults to print)
        bus: optional event bus; if provided, we also pub('apl_decision', ...)
        """
        self.player = player
        self.target = target
        self.is_cd_ready = is_cd_ready
        self.debug = debug
        self.log = logger or (lambda s: print(s))
        self.bus = bus
        self._last_action = None
        self.is_off_gcd = is_off_gcd
        self.time_until_ready_us = time_until_ready_us

    def _log_decision(self, *, action: str, reason: str, now_us: int):
        if self.debug == "off":
            return
        if self.debug == "unique" and action == self._last_action:
            return
        self._last_action = action

        p, t = self.player, self.target
        searing_rem = t.aura_remains_us("SearingBlaze", now_us)
        gcd_ready = now_us >= p.gcd_ready_us
        cast_ready = now_us >= p.busy_until_us

        # peek some readiness/charges for common CDs (safe even if absent)
        def ready(aid: str) -> bool:
            try: return self.is_ready(aid)
            except Exception: return False

        charges = {}
        for aid in ("fireball",):
            st = p.charges.get(aid)
            if st: charges[aid] = f"{st.cur}/{st.max}"

        msg = (f"[{us_to_s(now_us):7.3f}s] APL -> {action}"
               f" | reason={reason}"
               f" | ember={p.ember.cur}"
               f" | spirit={p.spiritbar.cur:.1f}"
               f" | searing_rem={us_to_s(searing_rem):.2f}s"
               f" | wild={'Y' if ready('wildfire') else 'n'}"
               f" | fb_chg={charges.get('fireball','-')}"
        )
        #       f" | gcd={'ready' if gcd_ready else f'+{us_to_s(p.gcd_ready_us-now_us):.2f}s'}"
        #       f" | cast={'ready' if cast_ready else f'+{us_to_s(p.busy_until_us-now_us):.2f}s'}"
        self.log(msg)
        if self.bus:
            self.bus.pub("apl_decision",
                         t_us=now_us,
                         action=action,
                         reason=reason,
                         ember=p.ember.cur,
                         searing_rem_us=searing_rem,
                         gcd_ready=gcd_ready,
                         cast_ready=cast_ready,
                         charges=charges)

    def choose_offgcd(self, now_us: int) -> str | None:
        p, t = self.player, self.target
        if now_us < p.busy_until_us:  # can't weave during a cast/channel
            return None

        def _try(ids, reason: str) -> str | None:
            # Normalize input to an iterable of ids
            if isinstance(ids, (str, bytes)):
                ids_iter = (ids,)
            else:
                try:
                    ids_iter = tuple(ids)
                except TypeError:
                    ids_iter = (ids,)

            for aid in ids_iter:
                # Only consider abilities that are off-GCD and ready
                if not self.is_off_gcd(aid) or not self.is_cd_ready(aid):
                    continue

                # Controls on ability usage:
                if aid == "wildfire" and (t.aura_remains_us("EngulfingFlames", now_us) <= 0
                                          or (t.aura_remains_us("Fireball", now_us)<=0 and self.time_until_ready_us("fireball")<=s_to_us(3.0))
                                          or (t.aura_remains_us("FrogDot", now_us)<=0 and self.time_until_ready_us("fire_frogs")<=s_to_us(8.0))):
                    continue

                self._log_decision(action=aid, reason=reason, now_us=now_us)
                return aid
            return None

        # Off-GCD priority – you can use single ids or groups (lists/tuples) here:
        return (
                _try("wildfire", "Wildfire ready & Engulfing Active")
                # or _try(["trinket1","trinket2"], "use a trinket")
                or None
        )


    def choose(self, now_us: int) -> str | None:
        p, t = self.player, self.target
        # Only call choose() when both gates are clear (runner enforces this)
        if now_us < max(p.gcd_ready_us, p.busy_until_us):
            return None

        # Snap
        if p.ember.cur >= 450:
            self._log_decision(action="detonate", reason="Prevent Ember Overcap", now_us=now_us)
            return "detonate"

        if self.is_cd_ready("apocalypse"):
            self._log_decision(action="apocalypse", reason="Apocalypse Ready", now_us=now_us)
            return "apocalypse"

        # Engulfing when available
        if self.is_cd_ready("engulfing_flames") and t.aura_remains_us("EngulfingFlames", now_us) == 0:
            self._log_decision(action="engulfing_flames", reason="Engulfing Ready & Not Present", now_us=now_us)
            return "engulfing_flames"

        # Fireball when available and not already present
        if self.is_cd_ready("fireball") and t.aura_remains_us("Fireball", now_us) == 0:
            self._log_decision(action="fireball", reason="Fireball Ready & Not Present", now_us=now_us)
            return "fireball"

        if self.is_cd_ready("fire_frogs"):
            self._log_decision(action="fire_frogs", reason="Frogs Ready", now_us=now_us)
            return "fire_frogs"

        # Searing Blaze maintenance on target
        if t.aura_remains_us("SearingBlaze", now_us) <= s_to_us(0.0):
            self._log_decision(action="searing_blaze", reason="Searing Blaze Not Present", now_us=now_us)
            return "searing_blaze"

        if self.is_cd_ready("incinerate") and (self.player.spiritbar.cur >= 96) and (self.player.ember.cur > 200):
            self._log_decision(action="detonate", reason="Spending Down to Incinerate", now_us=now_us)
            return "detonate"

        if self.is_cd_ready("incinerate") and (self.player.spiritbar.cur >= 100) and (self.player.ember.cur <= 200):
            self._log_decision(action="incinerate", reason="Spirit Charged & Embers Low Enough", now_us=now_us)
            return "incinerate"

        if self.is_cd_ready("wildfire") and not p.has_buff("Wildfire") and t.aura_remains_us("EngulfingFlames", now_us) >= 3:
            self._log_decision(action="wildfire", reason="Wildfire ready & Engulfing Active", now_us=now_us)
            return "wildfire"

        if p.ember.cur >= 100 and t.aura_remains_us("EngulfingFlames", now_us) > 0:
            self._log_decision(action="detonate", reason="Embers Available & Engulfing Present", now_us=now_us)
            return "detonate"

        # Infernal Wave filler
        self._log_decision(action="infernal_wave", reason="No Other Actions Available", now_us=now_us)
        return "infernal_wave"
