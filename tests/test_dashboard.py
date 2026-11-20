import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion.cogs.dashboard import months_to_max


def test_pace_estimates_match_spec():
    active = 20 * 15 + 150 + 60       # 20 msgs at 15 avg + daily bonus + 1h voice (6 blocks of 10 XP)
    casual = 10 * 15 + 150 + 30
    assert months_to_max(active) == "~12.7 months"
    assert months_to_max(casual) == "~19.6 months"
    assert months_to_max(0) == "never"
