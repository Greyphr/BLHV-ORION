"""/ai kb import | export, /ai brief, /ai gaps and /ai status: the commands staff use to teach Orion. No database needed."""
import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion import ai_brief
from orion.cogs.ai_admin import AIAdmin

TEMPLATE_OK = """# CORE BRIEF
Blue Heaven is a gaming Sect.

## Server rules
Be respectful.

## Esports and teams
<<FILL IN>>
"""


class FakeDB:
    """The few database calls the /ai commands make, kept in memory."""

    def __init__(self, kb=None, cfg=None):
        self.kb = kb or []
        self.cfg, self.audit, self.gaps, self._next = cfg or {}, [], [], 100

    def get(self, k, d=None): return self.cfg.get(k, d)
    async def set(self, k, v): self.cfg[k] = v
    async def kb_list(self, enabled_only=False): return [dict(r) for r in self.kb if r["enabled"] or not enabled_only]

    async def kb_add(self, title, content, by):
        self._next += 1
        self.kb.append({"id": self._next, "title": title, "content": content, "enabled": True})
        return self._next

    async def kb_update(self, eid, title, content):
        for r in self.kb:
            if r["id"] == eid: r.update(title=title, content=content)
        return True

    async def record_audit(self, actor, action, *a, **kw): self.audit.append((action, kw))
    async def ai_gap_logs(self, limit=15): return self.gaps


def cog_with(db):
    return AIAdmin(NS(db=db))


def call(cmd, cog, *args):
    f = getattr(cmd, "callback", None)               # a real slash command object, or a plain method
    return f(cog, *args) if f else cmd(*args)


def fake_i():
    return NS(user=NS(id=1), response=NS(send_message=AsyncMock(), defer=AsyncMock()), followup=NS(send=AsyncMock()))


def attachment(name, text, size=None):
    data = text.encode("utf-8")
    return NS(filename=name, size=len(data) if size is None else size, read=AsyncMock(return_value=data))


def run(coro):
    return asyncio.run(coro)


def test_import_loads_brief_and_entries_and_skips_unfinished_sections():
    db = FakeDB(); cog = cog_with(db); i = fake_i()
    run(call(cog.kb_import, cog, i, attachment("knowledge.md", TEMPLATE_OK)))
    assert db.cfg[ai_brief.BRIEF_KEY] == "Blue Heaven is a gaming Sect."
    assert [(r["title"], r["content"]) for r in db.kb] == [("Server rules", "Be respectful.")]
    said = i.followup.send.call_args.args[0]
    assert "1 added, 0 updated" in said and "core brief saved" in said and "Esports and teams" in said
    assert db.audit[0][0] == "ai.kb.import" and db.audit[0][1]["added"] == 1


def test_importing_an_edited_file_again_updates_instead_of_duplicating():
    db = FakeDB(); cog = cog_with(db)
    run(call(cog.kb_import, cog, fake_i(), attachment("k.md", "## Server rules\nBe respectful.")))
    i = fake_i()
    run(call(cog.kb_import, cog, i, attachment("k.md", "## SERVER RULES\nBe respectful. No spam.\n\n## Giveaways\nUse the button.")))
    assert len(db.kb) == 2 and db.kb[0]["content"] == "Be respectful. No spam."          # same title (any case) = update
    assert "1 added, 1 updated" in i.followup.send.call_args.args[0]


def test_import_refuses_the_wrong_file_type_a_huge_file_and_an_empty_template():
    db = FakeDB(); cog = cog_with(db)
    i = fake_i(); run(call(cog.kb_import, cog, i, attachment("rules.pdf", "x")))
    assert ".md" in i.response.send_message.call_args.args[0]
    i = fake_i(); run(call(cog.kb_import, cog, i, attachment("big.md", "## A\nb", size=10_000_000)))
    assert "too big" in i.response.send_message.call_args.args[0]
    i = fake_i(); run(call(cog.kb_import, cog, i, attachment("t.md", "## A\n<<FILL IN>>\n")))
    assert "Nothing to import" in i.followup.send.call_args.args[0]
    assert db.kb == [] and ai_brief.BRIEF_KEY not in db.cfg


def test_import_reads_a_file_saved_with_a_byte_order_mark():
    db = FakeDB(); cog = cog_with(db)
    att = NS(filename="k.txt", size=40, read=AsyncMock(return_value="\ufeff## Rules\nBe kind.".encode("utf-8")))
    run(call(cog.kb_import, cog, fake_i(), att))
    assert [(r["title"], r["content"]) for r in db.kb] == [("Rules", "Be kind.")]


def test_export_gives_back_a_file_that_imports_to_the_same_thing():
    db = FakeDB(cfg={ai_brief.BRIEF_KEY: "We play."}, kb=[{"id": 1, "title": "Rules", "content": "Be kind.", "enabled": True}])
    cog = cog_with(db); i = fake_i()
    run(call(cog.kb_export, cog, i))
    f = i.response.send_message.call_args.kwargs["file"]
    assert f.filename == "blue-heaven-knowledge.md"
    r = ai_brief.parse_knowledge_file(f.fp.read().decode("utf-8"))
    assert r.brief == "We play." and r.entries == [("Rules", "Be kind.")]


def test_brief_show_and_status_hint():
    db = FakeDB(); cog = cog_with(db)
    i = fake_i(); run(call(cog.brief_show, cog, i))
    assert "No core brief yet" in i.response.send_message.call_args.args[0]
    db.cfg[ai_brief.BRIEF_KEY] = "We play."
    i = fake_i(); run(call(cog.brief_show, cog, i))
    assert i.response.send_message.call_args.args[0] == "We play."


def test_gaps_lists_what_members_asked_that_orion_could_not_answer():
    db = FakeDB(); cog = cog_with(db)
    i = fake_i(); run(call(cog.gaps, cog, i))
    assert "No gaps" in i.response.send_message.call_args.args[0]
    db.gaps = [{"created_at": datetime(2026, 11, 24, tzinfo=timezone.utc), "ticket_id": 42,
                "summary": "Asks when esports tryouts open."}]
    i = fake_i(); run(call(cog.gaps, cog, i))
    text = i.response.send_message.call_args.args[0]
    assert "ticket #42" in text and "tryouts" in text and "/ai kb import" in text
