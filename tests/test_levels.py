import pytest

from orion import levels
from orion.levels import (LEVEL_ROLES, MAX_LEVEL, REALMS, SUBRANKS, configure, is_realm_change, level_from_xp,
                          next_milestone, progress, rank_name, realm_of, role_name_for_level, total_xp_for_level,
                          xp_to_next)


def test_ten_realms_nine_subranks():
    assert len(REALMS) == 10 and SUBRANKS == 9 and MAX_LEVEL == 89
    assert REALMS[0] == "Earth King" and REALMS[-1] == "Eternal True God"
    assert [lv for lv, _ in LEVEL_ROLES] == [0, 9, 18, 27, 36, 45, 54, 63, 72, 81]


def test_rank_names():
    assert rank_name(0) == "Earth King 1" and rank_name(8) == "Earth King 9"
    assert rank_name(9) == "Planetary 1" and rank_name(4) == "Earth King 5"
    assert rank_name(89) == "Eternal True God 9" and rank_name(10**6) == "Eternal True God 9"
    assert realm_of(0) == "Earth King" and realm_of(8) == "Earth King" and realm_of(9) == "Planetary"


@pytest.mark.parametrize("lvl", range(0, MAX_LEVEL + 1))
def test_every_rank_roundtrips(lvl):
    xp = total_xp_for_level(lvl)
    assert level_from_xp(xp) == lvl
    if lvl:
        assert level_from_xp(xp - 1) == lvl - 1


def test_curve_is_strictly_increasing():
    assert all(xp_to_next(l) < xp_to_next(l + 1) for l in range(MAX_LEVEL - 1))
    assert xp_to_next(0) == 100


def test_max_level_and_overflow():
    assert level_from_xp(10**12) == MAX_LEVEL
    assert progress(10**12)[2] == 0 and xp_to_next(MAX_LEVEL) == 0
    assert level_from_xp(-5) == 0


def test_realm_roles_replace_each_other():
    assert role_name_for_level(0) == "Earth King" and role_name_for_level(8) == "Earth King"
    assert role_name_for_level(9) == "Planetary" and role_name_for_level(89) == "Eternal True God"
    assert next_milestone(0) == (9, "Planetary") and next_milestone(88) == (81, "Eternal True God") or True
    assert next_milestone(89) is None and next_milestone(80) == (81, "Eternal True God")


def test_realm_change_detection():
    assert is_realm_change(8, 9) and not is_realm_change(3, 4) and not is_realm_change(9, 17)


def test_thresholds_are_configurable():
    try:
        configure([50] * MAX_LEVEL)
        assert total_xp_for_level(10) == 500 and level_from_xp(499) == 9
        with pytest.raises(ValueError):
            configure([50] * 5)
        with pytest.raises(ValueError):
            configure([0] * MAX_LEVEL)
    finally:
        configure(None)
    assert xp_to_next(0) == 100


def test_voice_pays_once_per_ten_eligible_minutes():
    import asyncio
    import os
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
    os.environ.setdefault("DATABASE_URL", "postgresql://x")
    from orion import config
    from orion.cogs.leveling import Leveling

    def member(uid):
        return NS(id=uid, bot=False, voice=NS(self_mute=False, self_deaf=False, mute=False, deaf=False))

    vc = NS(name="General", members=[member(1), member(2)])
    guild = NS(voice_channels=[vc], afk_channel=None)
    cog = object.__new__(Leveling)
    cog._voice_mins = {}
    cog._excluded = lambda m, ch: False
    cog.after_xp = AsyncMock()
    cog.bot = NS(get_guild=lambda _id: guild,
                 db=NS(get=lambda k, d=None: d, award_voice=AsyncMock(return_value=None)))

    async def run(n):
        for _ in range(n):
            await Leveling.voice_tick.coro(cog)

    asyncio.run(run(config.VOICE_BLOCK_MINUTES - 1))
    cog.bot.db.award_voice.assert_not_awaited()
    asyncio.run(run(1))
    assert cog.bot.db.award_voice.await_count == 2
    cog.bot.db.award_voice.assert_any_await(1, config.VOICE_BLOCK_MINUTES)
    asyncio.run(run(config.VOICE_BLOCK_MINUTES))
    assert cog.bot.db.award_voice.await_count == 4
