from sim.core.apl import SimpleAPL  # reuse your APL class
def make_apl(player, target, helpers):
    # Wire helpers into your SimpleAPL constructor as you already do
    return SimpleAPL(
        player, target,
        is_cd_ready=helpers["is_cd_ready"],
        is_off_gcd=helpers["is_off_gcd"],
        time_until_ready_us=helpers["time_until_ready_us"],
        debug="unique", logger=None, bus=player.bus
    )