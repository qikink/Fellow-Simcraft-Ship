from sim.core.apl import SimpleAPL  # reuse your APL class
def make_apl(player, target, world, helpers):
    # Wire helpers into your SimpleAPL constructor as you already do
    apl =  SimpleAPL(
        player, target, world,
        is_cd_ready=helpers["is_cd_ready"],
        is_off_gcd=helpers["is_off_gcd"],
        time_until_ready_us=helpers["time_until_ready_us"],
        debug="all", logger=None, bus=player.bus
    )
    apl.count_enemies = helpers["count_enemies"]
    apl.count_aura = helpers["count_aura"]
    apl.next_enemy_missing_aura = helpers["next_enemy_missing_aura"]
    apl.enemies_alive = helpers["enemies_alive"]
    return apl