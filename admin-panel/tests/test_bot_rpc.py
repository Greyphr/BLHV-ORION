import os

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DB_PASSWORD", "test-password")

from orion.bot_rpc import BotRPCServer, coerce_config_value


def test_cog_lookup_accepts_group_names():
    ticket_cog = object()

    class FakeBot:
        cogs = {"ticket": ticket_cog}

        def get_cog(self, _name):
            return None

    server = BotRPCServer(FakeBot(), token="test-token")
    assert server._cog("ticket") is ticket_cog
    assert server._cog("TICKET") is ticket_cog
    assert server._cog("missing") is None


def test_config_values_keep_discord_admin_types():
    assert coerce_config_value("dm_color", "5790199") == 5790199
    assert coerce_config_value("dm_color", "") == ""
    assert coerce_config_value("welcome_enabled", True) is True


@pytest.mark.asyncio
async def test_ticket_type_reload_redraws_ticket_panel():
    class FakeCog:
        def __init__(self):
            self.reloaded = False
            self.refreshed = False

        async def reload_types(self):
            self.reloaded = True

        async def refresh_panel(self):
            self.refreshed = True
            return None

    cog = FakeCog()

    class FakeBot:
        cogs = {"ticket": cog}

        def get_cog(self, _name):
            return None

    result = await BotRPCServer(FakeBot(), token="test-token")._tickets_types_reload({}, None, 1)
    assert result == {"reloaded": True, "note": None}
    assert cog.reloaded and cog.refreshed


@pytest.mark.asyncio
async def test_xp_reset_deletes_user_instead_of_adjusting():
    calls = []

    class FakeDB:
        async def get_user(self, _user_id):
            return {"xp": 123}

        async def reset_user(self, _user_id):
            calls.append("reset")

        async def log_progression(self, *_args):
            calls.append("log")

    class FakeGuild:
        def get_member(self, _member_id):
            return None

    class FakeBot:
        db = FakeDB()

        def get_guild(self, _guild_id):
            return FakeGuild()

    result = await BotRPCServer(FakeBot(), token="test-token")._xp_adjust(
        {"member_id": 7, "mode": "reset"}, None, 9
    )
    assert result["old_xp"] == 123
    assert result["new_xp"] == 0
    assert calls == ["reset", "log"]
