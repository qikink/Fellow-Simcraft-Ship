# sim/runtime/char_listeners.py (new helper module, or tuck into talents.py if you prefer)
def attach_swallow_listener(player, bus, world,
                                 triggers=("freezing_torrent", "cold_snap"),
                                 buff_name="swallows",
                                  coeff=63.0, fanout_chance=0.35):
    """
    While the 'BurstingIce' buff is active on `player`, every cast_end of any id in `triggers`
    deals `hits` extra hits of `coeff` each. Each hit has `fanout_chance` to hit all enemies.
    """

    def do_bursting_hits(primary_target):
        eng = player.eng
        rng_prefix = "Swallow"
        # choose a sane target if event didn't provide one
        tgt0 = primary_target or (world.primary() if world else None)
        if tgt0 is None:
            return
        if not player.buffs["buff_name"]:
            return
        if player.buffs["buff_name"].props["stacks"] <= 0:
            return
        hits = player.buffs["buff_name"].props["stacks"]

        # pick your damage calc the same way your 'damage' component does
        def one_hit(target, i):
            dmg = coeff * player.power * player.buff_damage_mult()
            # roll crit the same way you do for direct hits
            did_crit = player.rng.roll("bursting_crit", player.current_crit())
            if did_crit:
                dmg *= 2.0
            # apply damage + publish for any subscribers
            player.add_damage(dmg, "Swallow")
            bus.pub("damage_done",
                    t_us=eng.t_us,
                    ability_id="swallow_proc",
                    step_type="damage",
                    target=target,
                    crit=did_crit,
                    amount=dmg)


        for i in range(hits):
            fanout = player.rng.roll(f"{rng_prefix}:{i}", fanout_chance)
            if fanout and world:
                for u in (world.enemies_alive() or []):
                    one_hit(u, i)
            else:
                one_hit(tgt0, i)

    # main hook: whenever a cast ends, if buff is up and ability is in triggers, proc
    def on_cast_end(ability_id=None, caster=None, target=None, **_):
        if caster is not player:
            return
        if ability_id not in triggers:
            return
        if "BurstingIce" not in player.buffs:
            return
        do_bursting_hits(target)

    bus.sub("cast_end", on_cast_end)
