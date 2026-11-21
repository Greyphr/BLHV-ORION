import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DB_PASSWORD", "test-password")

from orion.bot_rpc import BotRPCServer


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
