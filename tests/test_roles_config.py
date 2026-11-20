"""Role-ID access checks and the safer config parsing (no Discord connection needed)."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import discord

from orion import checks, config


def member(*role_ids, names=None, owner=False):
    roles = [SimpleNamespace(id=i, name=(names or {}).get(i, f"r{i}")) for i in role_ids]
    m = MagicMock(spec=discord.Member)                  # isinstance(m, discord.Member) is True
    m.roles = roles
    m.id = 5
    m.guild = SimpleNamespace(owner_id=5 if owner else 99)
    return m


class DB(dict):
    pass


def test_admin_by_id_not_name():
    fake = member(1, names={1: "Sect Admin"})                 # a look-alike role with the wrong ID
    assert not checks.is_admin(fake)
    assert checks.is_admin(member(config.ADMIN_ROLE_ID))
    assert checks.is_xp_manager(member(config.ADMIN_ROLE_ID))


def test_sect_patriarch_has_full_access():
    patriarch = member(); patriarch.id = config.OWNER_ID
    assert config.OWNER_ID == 985637303844225104
    assert checks.is_admin(patriarch) and checks.is_xp_manager(patriarch) and checks.is_staff(patriarch, DB())


def test_staff_role_is_staff():
    db = DB()
    assert config.GUILD_ID == 1562411368458485820 or os.environ.get("GUILD_ID") != ""
    assert checks.is_staff(member(1562412567547224125), db)
    assert checks.is_staff(member(config.ADMIN_ROLE_ID), db)
    assert not checks.is_staff(member(42), db)
    assert checks.is_staff(member(42), DB(staff_role_ids=[42]))       # roles picked in /admin still work too


def test_int_env_never_crashes(monkeypatch):
    monkeypatch.setenv("SOME_ID", "")
    assert config._int_env("SOME_ID", 7) == 7
    monkeypatch.setenv("SOME_ID", "oops")
    assert config._int_env("SOME_ID", 7) == 7
    monkeypatch.setenv("SOME_ID", "55")
    assert config._int_env("SOME_ID", 7) == 55


def test_database_url_encodes_password(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_PASSWORD", "p@ss/w:rd#?%")
    monkeypatch.setenv("DB_HOST", "db")
    assert config._database_url() == "postgresql://orion:p%40ss%2Fw%3Ard%23%3F%25@db:5432/orion"
