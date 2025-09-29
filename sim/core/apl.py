# sim/core/apl.py
from __future__ import annotations
from .engine import s_to_us, us_to_s,APL

class SimpleAPL:
    """
    Priority:
    """

    def __init__(self, player, target, world, talents, is_cd_ready, is_off_gcd,time_until_ready_us, *, debug: str = "off", logger=None, bus=None):
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
        self.world = world
        self.is_cd_ready = is_cd_ready
        self.debug = debug
        self.log = logger or (lambda s: print(s))
        self.bus = bus
        self._last_action = None
        self.is_off_gcd = is_off_gcd
        self.time_until_ready_us = time_until_ready_us
        self.talents = talents

    def _log_decision(self, *, action: str, reason: str, now_us: int,target: str=""):
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

        msg = (f"[{us_to_s(now_us):7.3f}s] APL -> {action},{target}"
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
        n = self.count_enemies()
        searing_cov = self.count_aura("SearingBlaze")
        engulfing_cov = self.count_aura("EngulfingFlames")
        t = self.world.primary() if self.world else None
        if now_us < max(p.gcd_ready_us, p.busy_until_us):
            return None

        tt_fireball = us_to_s(self.time_until_ready_us("fireball"))
        tt_pyromania = us_to_s(self.time_until_ready_us("pyromania"))
        tt_engulfing_flames = us_to_s(self.time_until_ready_us("engulfing_flames"))
        tt_frogs = us_to_s(self.time_until_ready_us("fire_frogs"))
        tt_wildfire = us_to_s(self.time_until_ready_us("wildfire"))

        tt_engulfing_eff = min(tt_engulfing_flames,tt_pyromania)
        est_ramp = (100 - self.player.spiritbar.cur) * 1.5
        est_ramp_us = s_to_us(est_ramp + 1.5)

        delay_fireball = max(0, min(est_ramp_us - t.aura_remains_us("Fireball", now_us), tt_fireball - est_ramp_us))
        delay_engulfing = max(0,min(est_ramp_us - t.aura_remains_us("Engulfing", now_us), tt_engulfing_eff - est_ramp_us))
        delay_frogs = max(0, min(est_ramp_us - t.aura_remains_us("FrogDot", now_us), tt_frogs - est_ramp_us))
        delay_wildfire = max(0, tt_wildfire - 9)
        true_est_ramp = us_to_s(max(est_ramp_us, delay_fireball, delay_engulfing, delay_frogs, delay_wildfire)) #compute likely incinerate time, pool cd's if approaching

        #print("Estimating ",true_est_ramp," till Incinerate.")
        #print("FB:",delay_fireball,"eng:",delay_engulfing,"frogs:",delay_frogs,"wildfire:",delay_wildfire)
        if self.is_cd_ready("apocalypse") and n>1: #only apocalypse in AoE
            self._log_decision(action="apocalypse", reason="Apocalypse Ready", now_us=now_us,target=t.name)
            return ("apocalypse",t)

        if t.aura_remains_us("SearingBlaze", now_us) <= s_to_us(2.5) and "3A" in self.talents:
            self._log_decision(action="searing_blaze", reason="Overlap Searing Blaze for Intensifying", now_us=now_us, target=t.name)
            return ("searing_blaze",t)

        #ramp rotation:
        if true_est_ramp<=4.5:
            if t.aura_remains_us("SearingBlaze", now_us) >= s_to_us(1) and t.aura_remains_us("FrogDot",now_us) >= s_to_us(1) and t.aura_remains_us("Fireball", now_us) >= s_to_us(1) and t.aura_remains_us("EngulfingFlames",now_us) >= s_to_us(1) and (self.player.spiritbar.cur >= 100):
                self._log_decision(action="incinerate", reason="Fully Ramped Incinerate",
                                   now_us=now_us, target=t.name)
                return ("incinerate", t)

            if t.aura_remains_us("SearingBlaze", now_us) <= s_to_us(6):
                self._log_decision(action="searing_blaze", reason="Refresh Searing in Ramp",now_us=now_us, target=t.name)
                return ("searing_blaze", t)

            if self.is_cd_ready("fireball") and t.aura_remains_us("Fireball", now_us) <= s_to_us(4):
                self._log_decision(action="fireball", reason="Fireball in Ramp", now_us=now_us,
                                   target=t.name)
                return ("fireball", t)

            if self.is_cd_ready("fire_frogs"):
                self._log_decision(action="fire_frogs", reason="Frogs in Ramp", now_us=now_us, target=t.name)
                return ("fire_frogs", t)

            if self.is_cd_ready("engulfing_flames") and engulfing_cov < n:
                tgt = self.next_enemy_missing_aura("EngulfingFlames")
                self._log_decision(action="engulfing_flames", reason="Engulfing in Ramp", now_us=now_us,
                                   target=tgt.name)
                return ("engulfing_flames", tgt)

            if self.is_cd_ready("pyromania") and engulfing_cov < n:
                tgt = self.next_enemy_missing_aura("EngulfingFlames")
                self._log_decision(action="pyromania", reason="Engulfing (via Pyro) in Ramp", now_us=now_us,
                                   target=tgt.name)
                return ("pyromania", tgt)

            if p.ember.cur >= 150:
                self._log_decision(action="detonate", reason="Aggressive Detonate in Ramp", now_us=now_us,
                                   target=t.name)
                return ("detonate", t)

            self._log_decision(action="infernal_wave", reason="Fill in Ramp", now_us=now_us,
                               target=t.name)  # may need to wait 1-2 gcd's for all cooldowns to become available, or for spirit to charge
            return ("infernal_wave", t)

        # Pyro if many targets without engulfing
        if self.is_cd_ready("pyromania") and engulfing_cov<=n-3 and tt_engulfing_flames <= true_est_ramp+10:
            tgt = self.next_enemy_missing_aura("EngulfingFlames")
            self._log_decision(action="pyromania", reason="3+ Targets missing Engulfing", now_us=now_us,target=tgt.name)
            return ("pyromania",tgt)

        # Engulfing when available
        if self.is_cd_ready("engulfing_flames") and engulfing_cov<n and (tt_pyromania <= true_est_ramp or 20 <= true_est_ramp+8):
            tgt = self.next_enemy_missing_aura("EngulfingFlames")
            self._log_decision(action="engulfing_flames", reason="Engulfing Ready & Not Present", now_us=now_us,target=tgt.name)
            return ("engulfing_flames",tgt)

        # Pyromania if Engulfing not available & Engulfing not present
        if self.is_cd_ready("pyromania") and engulfing_cov<n and tt_engulfing_flames <= true_est_ramp:
            self._log_decision(action="pyromania", reason="Pyromania Ready & Engulfing Not Present", now_us=now_us,target=t.name)
            return ("pyromania",t)

        # Fireball when available and not already present
        if self.is_cd_ready("fireball") and t.aura_remains_us("Fireball", now_us) == 0 and 30 <= true_est_ramp+10:
            self._log_decision(action="fireball", reason="Fireball Ready & Not Present", now_us=now_us,target=t.name)
            return ("fireball",t)

        if self.is_cd_ready("fire_frogs") and 60 <= true_est_ramp+15:
            self._log_decision(action="fire_frogs", reason="Frogs Ready", now_us=now_us,target=t.name)
            return ("fire_frogs",t)

        # Searing Blaze maintenance on target
        if searing_cov<n:  #t.aura_remains_us("SearingBlaze", now_us) <= s_to_us(0.0):
            tgt = self.next_enemy_missing_aura("SearingBlaze")
            self._log_decision(action="searing_blaze", reason="Searing Blaze Not Present", now_us=now_us, target=tgt.name)
            return ("searing_blaze",tgt)


        if t.aura_remains_us("SearingBlaze", now_us) >= s_to_us(1) and t.aura_remains_us("FrogDot", now_us) >= s_to_us(1) and t.aura_remains_us("Fireball", now_us) >= s_to_us(1) and t.aura_remains_us("EngulfingFlames",now_us) >= s_to_us(1) and (self.player.spiritbar.cur >= 100):
            self._log_decision(action="incinerate", reason="Fully Ramped Incinerate",
                               now_us=now_us, target=t.name)
            return ("incinerate", t)

        if self.is_cd_ready("wildfire") and not p.has_buff("Wildfire") and t.aura_remains_us("EngulfingFlames", now_us) >= 3 and 45<= true_est_ramp+10:
            self._log_decision(action="wildfire", reason="Wildfire ready & Engulfing Active", now_us=now_us,target=t.name)
            return ("wildfire",None)

        if p.ember.cur >= 100 and (t.aura_remains_us("EngulfingFlames", now_us) > 0 or t.aura_remains_us("Fireball", now_us) > 0 or t.aura_remains_us("FrogDot", now_us) > 0):
            self._log_decision(action="detonate", reason="Embers Available & DoT(s) Present", now_us=now_us,target=t.name)
            return ("detonate",t)

        if self.player.charges.get("fireball").cur==2:
            self._log_decision(action="fireball", reason="Fireball Charges Capped", now_us=now_us,target=t.name)
            return ("fireball",t)

        # Infernal Wave filler
        self._log_decision(action="infernal_wave", reason="No Other Actions Available", now_us=now_us,target=t.name)
        return ("infernal_wave",t)
