"""Orion's identity, the generated server facts, and the knowledge file (import / export). Pure logic, no Discord."""
from pathlib import Path

from orion import ai_brief
from orion.ai_brief import ENTRY_MAX, PLACEHOLDER, parse_knowledge_file, render_knowledge_file

TEMPLATE = Path(__file__).resolve().parent.parent / "knowledge" / "blue_heaven_knowledge_template.md"


def test_identity_is_blue_heavens_personal_assistant_bot():
    for guild in ("Blue Heaven", "Blue Heaven Sect | Official", ""):
        text = ai_brief.identity_block(guild)
        assert "Blue Heaven's personal assistant bot" in text and '"Orion"' in text
        assert "not a person" in text and "never discuss which AI company or model" in text
    assert "Blue Heaven Sect Discord server" in ai_brief.identity_block("Blue Heaven")
    assert 'the "Blue Heaven Sect | Official" Discord server' in ai_brief.identity_block("Blue Heaven Sect | Official")


FACTS = dict(ticket_types=[{"name": "General Support", "description": "Questions or problems", "enabled": True},
                           {"name": "Retired", "description": "old", "enabled": False},
                           {"name": "Other", "description": "", "enabled": True}],
             reopen_hours=24, autoclose_hours=72, warn_hours=24, realms=["Earth King", "Planetary", "Eternal True God"],
             subranks=9, msg_min=10, msg_max=20, cooldown=60, daily_bonus=150, voice_per_block=10, voice_block_minutes=10,
             voice_cap=1200, min_chars=4, member_commands=[("/rank", "shows your level card")])


def test_facts_come_from_the_live_settings():
    f = ai_brief.format_facts(**FACTS)
    assert "- General Support: Questions or problems" in f and "- Other" in f and "Retired" not in f
    assert "within 24 hours" in f and "no new messages for 72 hours" in f and "closes 24 hours later" in f
    assert "3 realms, from Earth King up to Eternal True God" in f and "Each realm has 9 sub-ranks" in f
    assert "10 to 20 XP, at most once every 60 seconds" in f and "+150 XP bonus" in f
    assert "10 XP for every 10 minutes" in f and "up to 1200 XP a day" in f
    assert "- /rank: shows your level card" in f
    f = ai_brief.format_facts(**{**FACTS, "reopen_hours": 0, "autoclose_hours": 0, "member_commands": []})
    assert "reopen" not in f and "reminder" not in f and "COMMANDS" not in f


SAMPLE = """Ignored text before any heading.

<!-- a comment that is not imported -->
# CORE BRIEF
Blue Heaven is a gaming Sect.

## Server rules
Be respectful.
No spam.

## Esports and teams
<<FILL IN>>

## Empty one

## Giveaways
Enter with the button.
"""


def test_parse_reads_brief_entries_and_skips_unfinished_sections():
    r = parse_knowledge_file(SAMPLE)
    assert r.brief == "Blue Heaven is a gaming Sect."
    assert r.entries == [("Server rules", "Be respectful.\nNo spam."), ("Giveaways", "Enter with the button.")]
    assert any("Esports and teams" in x and PLACEHOLDER in x for x in r.skipped)
    assert any("Empty one" in x and "empty" in x for x in r.skipped)
    assert "comment" not in " ".join(c for _, c in r.entries) and "Ignored text" not in (r.brief or "")


def test_parse_handles_windows_line_endings_and_alternate_brief_titles():
    r = parse_knowledge_file("## About Blue Heaven\r\nWe play.\r\n\r\n## Rules\r\nBe kind.\r\n")
    assert r.brief == "We play." and r.entries == [("Rules", "Be kind.")]
    assert parse_knowledge_file("").entries == [] and parse_knowledge_file(None).brief is None


def test_long_sections_are_split_into_parts_that_fit():
    body = "\n\n".join(f"Paragraph {i}. " + "word " * 80 for i in range(12))
    r = parse_knowledge_file("## FAQ\n" + body)
    assert len(r.entries) > 1 and r.entries[0][0] == "FAQ" and r.entries[1][0] == "FAQ (part 2)"
    assert all(len(c) <= ENTRY_MAX for _, c in r.entries)
    one = parse_knowledge_file("## Wall\n" + "x" * 4000)                      # a single unbroken paragraph is still cut
    assert all(len(c) <= ENTRY_MAX for _, c in one.entries) and "".join(c for _, c in one.entries) == "x" * 4000


def test_too_long_a_brief_and_too_many_entries_are_refused_not_truncated_silently():
    r = parse_knowledge_file("# CORE BRIEF\n" + "a" * 4001)
    assert r.brief is None and "4,001 characters" in r.skipped[0]
    many = parse_knowledge_file("\n".join(f"## T{i}\nbody {i}" for i in range(ai_brief.MAX_IMPORT_ENTRIES + 5)))
    assert len(many.entries) == ai_brief.MAX_IMPORT_ENTRIES and "5 entries past" in many.skipped[-1]


def test_export_then_import_round_trips():
    entries = [("Server rules", "Be respectful.\nNo spam."), ("Giveaways", "Enter with the button.")]
    r = parse_knowledge_file(render_knowledge_file("We play.", entries))
    assert r.brief == "We play." and r.entries == entries and r.skipped == []
    empty = parse_knowledge_file(render_knowledge_file("", []))
    assert empty.brief is None and empty.entries == [] and PLACEHOLDER in empty.skipped[0]      # nothing written yet = nothing imported


def test_the_starter_template_imports_nothing_until_it_is_filled_in():
    text = TEMPLATE.read_text(encoding="utf-8")
    r = parse_knowledge_file(text)
    assert r.brief is None and r.entries == [] and len(r.skipped) >= 10
    filled = text.replace(PLACEHOLDER, "Real answer.")
    r = parse_knowledge_file(filled)
    assert r.brief == "Real answer." and len(r.entries) >= 10 and r.skipped == []
