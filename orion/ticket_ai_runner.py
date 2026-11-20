"""Runs the AI ticket handler inside the ticket cog: watches a ticket, asks the model, applies the decision.

Hybrid model: the AI handles routine questions; anything sensitive, uncertain or unanswerable is handed to staff, and the
AI steps back as soon as a staff member speaks or claims the ticket. It has no permissions of its own: everything it can
do is the handful of actions below (send a message, hand over, close a routine ticket the member says is solved).
"""
import asyncio
import logging
from collections import defaultdict

import discord

from . import ai, alerts, ticket_ai
from .ticket_ai import Decision, build_system_prompt, build_user_message, decide, load_settings, parse_model_json, \
    select_knowledge

log = logging.getLogger("orion.ticket.ai")

DEBOUNCE_SECONDS = 4          # wait for the member to finish typing several messages
GREETING = ("Hey {mention}! 👋 I'm **Orion**, the Sect's assistant. I'll try to help while the team gets ready. "
            "A staff member can step in at any time.")
HANDOFF = ("🙋 I'm passing this ticket to the Sect staff so you get the right answer. "
           "Please add any extra details here and they'll reply as soon as they can.")
HANDOFF_SENSITIVE = ("🙋 Thanks for opening this ticket. This kind of matter is handled by the Sect staff, so I've notified "
                     "them. Please add any details or proof here and they'll reply as soon as they can.")
STEP_BACK = "🌙 A staff member has joined, so I'll step back and leave this to them."
CLOSING = ("🌟 Glad I could help! I'm closing this ticket now. Your transcript will be saved, and you can **reopen** it "
           "from this channel if you need anything else.")
FAILED = ("Thanks for your message! I couldn't process that just now, so I've asked the staff to follow up.")


class TicketAIRunner:
    def __init__(self, cog):
        self.cog = cog
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: dict[int, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def db(self):
        return self.cog.db

    def settings(self):
        return load_settings(self.db.get)

    def cancel_all(self):
        for t in list(self._tasks):
            t.cancel()

    # ------------------------------------------------------------ entry points
    async def start(self, ch: discord.TextChannel, member: discord.abc.User, ticket_id: int, type_name: str):
        """A ticket was just created: greet, then answer the form (or hand a sensitive type straight to staff)."""
        s = self.settings()
        if not s.enabled:
            return
        human_only = type_name.strip().lower() in s.human_only
        if not human_only and not ai.is_configured():
            return
        await self.db.ai_set_state(ticket_id, "active")
        self._set_cache(ch.id, "active")
        await self.cog._say(ch.id, GREETING.format(mention=member.mention),
                            allowed_mentions=discord.AllowedMentions(users=[member]))
        if human_only:
            t = await self.db.ticket_get(ticket_id)
            await self._handoff(ch, t, Decision("escalate", reason="this ticket type is handled by humans only"), s,
                                sensitive_text=True)
        else:
            self.schedule(ch.id, delay=0)

    def schedule(self, channel_id: int, delay: float = DEBOUNCE_SECONDS):
        if channel_id in self._pending:
            return
        task = asyncio.create_task(self._run(channel_id, delay))
        self._pending[channel_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self, channel_id: int, ticket_id: int, reason: str, announce: bool = True):
        """A human took over (or staff switched the AI off for this ticket)."""
        self._set_cache(channel_id, "off")
        await self.db.ai_set_state(ticket_id, "off")
        await self.db.ai_log(ticket_id, "handoff", reason=reason)
        if announce:
            await self.cog._say(channel_id, STEP_BACK)

    async def set_for_ticket(self, t, on: bool) -> str:
        s = self.settings()
        if on:
            if not s.enabled:
                return "The AI helper is switched off globally. Turn it on in `/ai switch` first."
            if not ai.is_configured():
                return "No `AI_API_KEY` is set, so the AI can't answer."
            await self.db.ai_set_state(t["id"], "active")
            self._set_cache(t["channel_id"], "active")
            self.schedule(t["channel_id"], delay=0)
            await self.db.ai_log(t["id"], "skip", reason="switched on for this ticket by staff")
            return f"🤖 AI helper is **on** for ticket #{t['id']:04d}."
        await self.stop(t["channel_id"], t["id"], "switched off for this ticket by staff", announce=False)
        return f"⏸️ AI helper is **off** for ticket #{t['id']:04d}."

    def _set_cache(self, channel_id: int, state: str):
        o = self.cog.open.get(channel_id)
        if o is not None:
            o["ai"] = state

    # ----------------------------------------------------------------- a turn
    async def _run(self, channel_id: int, delay: float):
        try:
            if delay:
                await asyncio.sleep(delay)
            self._pending.pop(channel_id, None)
            async with self._locks[channel_id]:
                await self._turn(channel_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("AI ticket handler crashed in channel %s", channel_id)
        finally:
            self._pending.pop(channel_id, None) if self._pending.get(channel_id) is asyncio.current_task() else None

    def _alive(self, channel_id: int) -> bool:
        o = self.cog.open.get(channel_id)
        return bool(o and o.get("ai") == "active")

    async def _history(self, ch: discord.TextChannel, t) -> tuple[list[tuple[str, str]], bool]:
        """(oldest-first [(speaker, text)], whether the newest real message was Orion's own)."""
        out, newest_is_bot = [], None
        async for m in ch.history(limit=20):
            if m.webhook_id or not m.content:
                continue
            if newest_is_bot is None:
                newest_is_bot = m.author.id == self.cog.bot.user.id
            if m.author.id == self.cog.bot.user.id:
                who = "Orion"
            elif m.author.id == t["owner_id"]:
                who = "Member"
            elif self.cog.is_handler(m.author, t["type_id"]):
                who = "Staff"
            else:
                who = "Participant"
            out.append((who, m.clean_content))
        out.reverse()
        return out, bool(newest_is_bot)

    async def _turn(self, channel_id: int):
        if not self._alive(channel_id):
            return
        o = self.cog.open[channel_id]
        ch = self.cog.bot.get_channel(channel_id)
        t = await self.db.ticket_get(o["id"])
        if ch is None or t is None or t["status"] != "open" or t["ai_state"] != "active":
            return
        s = self.settings()
        if not s.enabled:
            return await self.stop(channel_id, t["id"], "AI switched off globally", announce=False)
        convo, newest_is_bot = await self._history(ch, t)
        if newest_is_bot:
            return                                   # already answered; wait for the member
        types = [x["name"] for x in self.cog.types.values() if x["enabled"]] or [t["type_name"]]
        form = " ".join(str(a.get("a", "")) for a in (t["answers"] or []))
        member_text = form + " " + " ".join(text for who, text in convo if who == "Member")
        kb = select_knowledge(await self.db.kb_list(True), member_text)
        system = build_system_prompt(s, type_name=t["type_name"], type_names=types, kb=kb,
                                     guild_name=ch.guild.name)
        user = build_user_message(t["answers"] or [], convo)
        try:
            async with ch.typing():
                text = await ai.complete(system, user, max_tokens=700, model=s.model or None, json_mode=True, raw=True)
        except ai.AIError as e:
            return await self._failed(ch, t, s, str(e))
        d = decide(parse_model_json(text), s, type_name=t["type_name"], type_names=types,
                   ai_replies=t["ai_replies"], staff_involved=bool(t["first_staff_reply_at"] or t["claimed_by"]))
        if not self._alive(channel_id):
            return                                   # a human took over while the model was thinking
        if d.action == "reply":
            await self.cog._say(channel_id, d.reply, allowed_mentions=discord.AllowedMentions.none())
            await self.db.ai_count_reply(t["id"], d.category or None)
            await self.db.ai_save_summary(t["id"], d.summary or None, d.category or None)
            await self.db.ai_log(t["id"], "reply", confidence=d.confidence, category=d.category, reason=d.reason,
                                 summary=d.summary)
        elif d.action == "close":
            await self._close(ch, t, d)
        else:
            await self._handoff(ch, t, d, s)

    # ------------------------------------------------------------------ actions
    async def _close(self, ch, t, d: Decision):
        self._set_cache(ch.id, "done")
        await self.db.ai_set_state(t["id"], "done")
        await self.db.ai_save_summary(t["id"], d.summary or None, d.category or None)
        await self.db.ai_log(t["id"], "close", confidence=d.confidence, category=d.category, reason=d.reason,
                             summary=d.summary)
        await self.cog._say(ch.id, CLOSING)
        fresh = await self.db.ticket_get(t["id"])
        reason = ("Resolved by Orion AI" + (f": {d.summary}" if d.summary else ""))[:500]
        await self.cog.close_ticket(fresh, None, reason)

    async def _handoff(self, ch, t, d: Decision, s, *, sensitive_text: bool = False):
        self._set_cache(ch.id, "escalated")
        await self.db.ai_set_state(t["id"], "escalated")
        await self.db.ai_save_summary(t["id"], d.summary or None, d.category or None)
        await self.db.ai_log(t["id"], "escalate", confidence=d.confidence, category=d.category, reason=d.reason,
                             summary=d.summary)
        e = discord.Embed(color=self.cog.guild_color(), title="🙋 Handing over to staff",
                          description=HANDOFF_SENSITIVE if sensitive_text else HANDOFF)
        if d.summary:
            e.add_field(name="Summary for staff", value=d.summary[:1000], inline=False)
        if d.category:
            e.add_field(name="Suggested category", value=d.category, inline=True)
        e.add_field(name="Why", value=d.reason[:200] or "—", inline=True)
        pings, roles = "", []
        if s.ping_staff:
            roles = [r for r in (ch.guild.get_role(rid) for rid in self.cog.handler_role_ids(t["type_id"])) if r]
            pings = " ".join(r.mention for r in roles)
        try:
            await ch.send(content=pings or None, embed=e, allowed_mentions=discord.AllowedMentions(roles=roles))
        except discord.HTTPException:
            log.warning("Could not post the handover in ticket %s", t["id"], exc_info=True)

    async def _failed(self, ch, t, s, error: str):
        await alerts.alert_staff(self.cog.bot, ch.guild, f"The AI ticket handler couldn't answer ticket #{t['id']:04d}: "
                                 f"{error} Staff were pinged in the ticket.", key="ai-helper-failed")
        await self.db.ai_log(t["id"], "error", reason=error)
        await self.cog._say(ch.id, FAILED)
        await self._handoff(ch, t, Decision("escalate", reason="the AI service failed"), s)
