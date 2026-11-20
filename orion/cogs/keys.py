"""Beta keys for Rivals Fantasy.

/keys         Beta Tester role (or anyone an admin gave codes to). The first run assigns the tester 3 codes (BETA_KEYS_PER_TESTER) and DMs them.
              Running it again only re-shows the SAME codes, in a reply nobody else can see. If DMs are closed, the codes
              are shown in that private reply instead. Codes are never posted in a channel.
/givekeys     Sect Admin + key manager roles. Gives a member, or every member of a role, a set number of codes from the regular or
              the content creator stock. Big batches go out as a .txt file.
/keystats     Sect Admin + key manager roles. Codes left, who holds how many, plus a CSV of everyone (never contains codes).
/importkeys   Sect Admin + key manager roles. Loads a CSV of one-use codes into the regular or the content creator stock. Codes are encrypted before they reach the database.
/resetkeys    Sect Admin only. Deletes the UNCLAIMED codes only; claimed codes and claim records are kept.

Every claim and grant is recorded in the database and posted (without any code) in the private staff log channel.
"""
import asyncio
import csv
import io
import logging
import time
from collections import defaultdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import alerts, config, vault
from ..checks import require_admin, require_keys_manager
from ..keys import FILE_NAME, KeysError, mask, parse_csv, render_codes

log = logging.getLogger("orion.keys")
utcnow = discord.utils.utcnow

LOW_STOCK = 100                       # staff get an alert when this few codes (or fewer) are left
MAX_CSV_BYTES = 8 * 1024 * 1024
NO_MENTIONS = discord.AllowedMentions.none()

DM_INTRO = ("🔑 **Your Rivals Fantasy beta keys**\n"
            "Each code works **once** and these are yours alone. Please don't share or post them anywhere public.\n"
            "Lost them? Run `/keys` in the server and you'll see the same codes again.")
GIVE_INTRO = ("🔑 **{n:,} Rivals Fantasy beta keys** from the Blue Heaven team.\n"
              "Each code works **once**. Please don't post them anywhere public.\n"
              "Lost them? Run `/keys` in the server and you'll see them again.")
RESHOW_INTRO = "🔑 **Your Rivals Fantasy beta keys.** These are the same codes as before, there are no new ones to claim."
DMS_CLOSED_INTRO = ("🔑 Your DMs are closed, so your keys are shown here instead. **Only you can see this message.**\n"
                    "Save them now. You can run `/keys` again any time to see the same codes.")
BUSY = "Keys are temporarily unavailable. Staff have been told, please try again later."


def csv_safe(text: str) -> str:
    """Stops a display name like "=HYPERLINK(...)" from being run as a formula when staff open the CSV in Excel."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def has_role(member, role_id: int) -> bool:
    return bool(role_id) and any(r.id == role_id for r in getattr(member, "roles", ()))


POOL_LABEL = {"regular": "regular", "creator": "content creator"}
POOL_CHOICES = [app_commands.Choice(name="Regular keys (testers)", value="regular"),
                app_commands.Choice(name="Content creator keys", value="creator")]
MAX_ROLE_RECIPIENTS = 500             # one /givekeys to a role stops at this many members (DM pacing, 15 min button limit)
DM_PAUSE = 0.6                        # seconds between DMs when giving to a whole role, so Discord doesn't rate-limit us


class _ConfirmGive(discord.ui.View):
    """Confirm step for /givekeys, so a typo like 5000 instead of 50 can't hand out thousands of codes."""

    def __init__(self, cog: "BetaKeys", admin_id: int, targets: list[discord.Member], amount: int, pool: str,
                 role: Optional[discord.Role] = None):
        super().__init__(timeout=120)
        self.cog, self.admin_id, self.targets, self.amount, self.pool, self.role = cog, admin_id, targets, amount, pool, role
        self._done = False

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id == self.admin_id:
            return True
        await i.response.send_message("Only the admin who ran this command can confirm it.", ephemeral=True)
        return False

    @discord.ui.button(label="Give keys", emoji="🔑", style=discord.ButtonStyle.success)
    async def confirm(self, i: discord.Interaction, _):
        if self._done:                                   # double click: the first press already did the work
            await i.response.defer()
            return
        self._done = True
        await i.response.edit_message(content="Working on it…", view=None)
        if self.role is None:
            await self.cog.do_give(i, self.targets[0], self.amount, self.pool)
        else:
            await self.cog.do_give_role(i, self.role, self.targets, self.amount, self.pool)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _):
        self._done = True
        await i.response.edit_message(content="Cancelled. No keys were given.", view=None)


class _ConfirmReset(discord.ui.View):
    """Confirm step for /resetkeys: it deletes the unclaimed codes, so it must never be one click."""

    def __init__(self, cog: "BetaKeys", admin_id: int, pool: Optional[str]):
        super().__init__(timeout=60)
        self.cog, self.admin_id, self.pool = cog, admin_id, pool
        self._done = False

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id == self.admin_id:
            return True
        await i.response.send_message("Only the person who ran this command can confirm it.", ephemeral=True)
        return False

    @discord.ui.button(label="Yes, delete the unclaimed codes", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _):
        if self._done:
            await i.response.defer()
            return
        self._done = True
        await i.response.edit_message(content="Working on it…", view=None)
        try:
            codes = await self.cog.bot.db.keys_reset(self.pool)
        except Exception:
            log.exception("resetkeys failed")
            await i.edit_original_response(content="⚠️ Something went wrong and nothing was changed. Try again.")
            return
        which = POOL_LABEL[self.pool] if self.pool else "regular and content creator"
        log.warning("Beta keys RESET (%s, unclaimed only) by %s (%d): %d codes deleted", which, i.user, i.user.id, codes)
        ch = self.cog._log_channel(i.guild)
        if ch is not None:
            e = discord.Embed(title="🗑️ Unclaimed beta keys cleared", color=config.COLOR_ACCENT, timestamp=utcnow(),
                              description=f"{i.user.mention} deleted the unclaimed **{which}** codes. Claimed codes were kept.")
            e.add_field(name="Codes deleted", value=f"**{codes:,}**", inline=True)
            try:
                await ch.send(embed=e, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                log.warning("Beta keys: couldn't post the reset to the log channel %s", ch)
        await i.edit_original_response(content=f"✅ Deleted **{codes:,}** unclaimed {which} codes. Every claimed code and claim "
                                               "record was kept. Load the real codes with `/importkeys`.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _):
        self._done = True
        await i.response.edit_message(content="Cancelled. Nothing was deleted.", view=None)


class BetaKeys(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._wrong_channel_logged: dict[int, float] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)    # one /keys at a time per person

    # ------------------------------------------------------------- helpers
    async def _vault_ok(self, i: discord.Interaction, admin: bool = False) -> bool:
        """The encryption key must be the one the stored codes were locked with, or nothing is touched."""
        try:
            await vault.verify(self.bot.db)
            return True
        except vault.VaultError as e:
            if i.guild:
                await alerts.alert_staff(self.bot, i.guild, f"Beta keys: {e}", key="keys-vault")
            await i.followup.send(f"⚠️ {e}" if admin else BUSY, ephemeral=True)
            return False

    @staticmethod
    def _decrypt(blobs: list[bytes]) -> list[str]:
        return [vault.decrypt(b) for b in blobs]

    @staticmethod
    def _encrypt(codes: list[str]) -> list[tuple[str, bytes]]:
        return [(vault.blind_index(c), vault.encrypt(c)) for c in codes]

    async def _decrypt_async(self, blobs: list[bytes]) -> list[str]:
        return await asyncio.to_thread(self._decrypt, blobs)     # thousands of codes shouldn't stall the bot

    @staticmethod
    def _code_file(codes: list[str]) -> discord.File:
        return discord.File(io.BytesIO("\n".join(codes).encode("utf-8")), filename=FILE_NAME)

    async def _dm_codes(self, user: discord.abc.User, codes: list[str], intro: str) -> bool:
        """DM the codes (inline, or as a .txt file for big batches). False if the DMs are closed or the send failed."""
        text, inline = render_codes(codes)
        try:
            if inline:
                await user.send(f"{intro}\n```\n{text}\n```")
            else:
                await user.send(intro, file=self._code_file(codes))
            return True
        except discord.HTTPException:                           # Forbidden (DMs closed) is a kind of HTTPException
            return False

    async def _private_codes(self, i: discord.Interaction, codes: list[str], intro: str):
        """Show the codes in a reply only the person who ran the command can see."""
        text, inline = render_codes(codes)
        if inline:
            await i.followup.send(f"{intro}\n```\n{text}\n```", ephemeral=True)
        else:
            await i.followup.send(intro, file=self._code_file(codes), ephemeral=True)

    def _log_channel(self, guild: discord.Guild):
        for cid in (config.BETA_KEYS_LOG_CHANNEL_ID, self.bot.db.get("alert_channel_id", 0)):
            ch = guild.get_channel(cid) if cid else None
            if ch is not None:
                return ch
        return None

    async def _after_claim(self, guild: discord.Guild, user: discord.abc.User, amount: int, kind: str,
                           by: Optional[discord.abc.User], at, remaining: int, dm_sent: bool):
        """Staff log line (never contains a code) + low-stock warning. Neither can undo or block the claim itself:
        the permanent record is the database, this is the staff-facing copy."""
        ch = self._log_channel(guild)
        if ch is None:
            log.warning("Beta keys: no log channel is set; claim by %s (%d) is only in the database", user, user.id)
        else:
            if kind == "self":
                title, how = "🔑 Beta keys claimed", "Claimed with `/keys`"
            else:
                title, how = "🔑 Beta keys given", f"Given by {by.mention} with `/givekeys`" if by else "Given by an admin"
            e = discord.Embed(title=title, color=config.COLOR_ACCENT, timestamp=at or utcnow())
            e.add_field(name="Member", value=f"{user.mention}\n`{user.id}`", inline=True)
            e.add_field(name="Keys", value=f"**{amount:,}**", inline=True)
            e.add_field(name="Delivered", value="By DM" if dm_sent else "Private reply (DMs closed)", inline=True)
            e.add_field(name="How", value=how, inline=False)
            e.set_footer(text=f"{remaining:,} codes left in stock")
            try:
                await ch.send(embed=e, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                log.warning("Beta keys: couldn't post to the log channel %s", ch)
                await alerts.alert_staff(self.bot, guild, f"I couldn't post to the beta keys log channel ({ch.mention}). "
                                         "Claims are still saved, see `/keystats`.", key="keys-log-channel")
        if remaining <= LOW_STOCK:
            await alerts.alert_staff(self.bot, guild, f"Beta keys are running low: **{remaining:,}** codes left. "
                                     "Ask Banshee for more and load them with `/importkeys`.", key="beta-keys-low")

    async def _log_use(self, i: discord.Interaction, outcome: str, first_claim: bool = False):
        """Every time a Beta Tester runs /keys, post who it was and what happened (never a code) in the staff alerts
        channel. A first claim is already posted by _after_claim, so it is skipped when that went to the same channel."""
        ch = i.guild.get_channel(self.bot.db.get("alert_channel_id", 0)) if i.guild else None
        if ch is None:
            return
        if first_claim:
            logged = self._log_channel(i.guild)
            if logged is not None and logged.id == ch.id:
                return
        e = discord.Embed(title="🔑 /keys used", color=config.COLOR_MAIN, timestamp=utcnow())
        e.add_field(name="Tester", value=f"{i.user.mention}\n`{i.user.id}`", inline=True)
        e.add_field(name="Result", value=outcome, inline=True)
        e.add_field(name="Channel", value=f"<#{i.channel_id}>", inline=True)
        try:
            await ch.send(embed=e, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            log.warning("Beta keys: couldn't post the /keys use to the alerts channel %s", ch)

    # ---------------------------------------------------------------- /keys
    @app_commands.command(name="keys", description="Get your Rivals Fantasy beta keys")
    @app_commands.guild_only()
    async def keys(self, i: discord.Interaction):
        # Beta Testers may only use /keys in the designated channel.
        if (config.BETA_KEYS_CHANNEL_ID and i.channel_id != config.BETA_KEYS_CHANNEL_ID
                and has_role(i.user, config.BETA_TESTER_ROLE_ID)):
            await i.response.send_message(f"Please use `/keys` in <#{config.BETA_KEYS_CHANNEL_ID}>.", ephemeral=True)
            now = time.monotonic()                      # at most one log line per person per 5 minutes, so spamming can't flood it
            if now - self._wrong_channel_logged.get(i.user.id, -1e9) >= 300:
                self._wrong_channel_logged[i.user.id] = now
                await self._log_use(i, f"❌ Tried it in the wrong channel (only <#{config.BETA_KEYS_CHANNEL_ID}> works). No keys given")
            return
        await i.response.defer(ephemeral=True)
        if not await self._vault_ok(i):
            return
        db, member = self.bot.db, i.user
        is_tester = has_role(member, config.BETA_TESTER_ROLE_ID)
        async with self._locks[member.id]:
            if is_tester:
                res = await db.keys_claim(member.id, config.BETA_KEYS_PER_TESTER, "self")
                if res.status == "ok":
                    codes = await self._decrypt_async(res.codes)
                    sent = await self._dm_codes(member, codes, DM_INTRO)
                    await self._after_claim(i.guild, member, len(codes), "self", None, res.at, res.remaining, sent)
                    await self._log_use(i, f"Claimed **{len(codes)}** keys for the first time ({'sent by DM' if sent else 'DMs closed, shown in reply'})",
                                        first_claim=True)
                    if sent:
                        await i.followup.send(f"✅ I've sent your **{len(codes)}** beta keys to your DMs.", ephemeral=True)
                    else:
                        await self._private_codes(i, codes, DMS_CLOSED_INTRO)
                    return
                if res.status == "short":
                    await alerts.alert_staff(
                        self.bot, i.guild, f"A Beta Tester ran `/keys` but only **{res.remaining:,}** codes are left "
                        f"(they need {config.BETA_KEYS_PER_TESTER}). Load more with `/importkeys`.", key="beta-keys-empty")
                    await self._log_use(i, "Tried to claim but the stock is out")
                    await i.followup.send("😔 We're out of beta keys for the moment. Staff have been told, "
                                          "please try again later. You haven't lost your spot.", ephemeral=True)
                    return
                # status 'already': they claimed before, fall through and show the same codes again

            blobs = await db.keys_codes_of(member.id)
            if blobs:
                await self._private_codes(i, await self._decrypt_async(blobs), RESHOW_INTRO)
                if is_tester:
                    await self._log_use(i, f"Viewed their **{len(blobs)}** keys again")
            elif is_tester:
                await self._log_use(i, "Has the role but no codes found")
                await i.followup.send("I can't find your codes. Please open a ticket so staff can check.", ephemeral=True)
            elif has_role(member, config.BETA_CREATOR_ROLE_ID):
                await i.followup.send("Your creator keys haven't been set up yet. Ask staff to give you your batch "
                                      "and then run `/keys` again.", ephemeral=True)
            else:
                await i.followup.send("Beta keys are for members with the **Beta Tester** role.", ephemeral=True)

    # ------------------------------------------------------------ /givekeys
    @app_commands.command(name="givekeys", description="Give beta keys to a member, or to everyone with a role (admins only)")
    @app_commands.describe(amount="How many codes (each person gets this many)", user="Give to this member...",
                           role="...or to everyone with this role", type="Which stock the codes come from")
    @app_commands.choices(type=POOL_CHOICES)
    @app_commands.guild_only()
    @app_commands.check(require_keys_manager)
    async def givekeys(self, i: discord.Interaction, amount: app_commands.Range[int, 1, 100000],
                       user: Optional[discord.Member] = None, role: Optional[discord.Role] = None,
                       type: Optional[app_commands.Choice[str]] = None):
        await i.response.defer(ephemeral=True)
        pool = type.value if type else "regular"
        if (user is None) == (role is None):
            await i.followup.send("Pick **either** a `user` **or** a `role`, not both and not neither.", ephemeral=True)
            return
        if user is not None:
            if user.bot:
                await i.followup.send("Bots can't use beta keys.", ephemeral=True)
                return
            targets = [user]
        else:
            if role.is_default():
                await i.followup.send("I won't give keys to @everyone. Pick a specific role.", ephemeral=True)
                return
            targets = [m for m in role.members if not m.bot]
            if not targets:
                await i.followup.send(f"Nobody (apart from bots) has {role.mention}.", ephemeral=True,
                                      allowed_mentions=NO_MENTIONS)
                return
            if len(targets) > MAX_ROLE_RECIPIENTS:
                await i.followup.send(f"{role.mention} has {len(targets):,} members. The limit for one go is "
                                      f"{MAX_ROLE_RECIPIENTS}. Use a smaller role.", ephemeral=True, allowed_mentions=NO_MENTIONS)
                return
        if not await self._vault_ok(i, admin=True):
            return
        db = self.bot.db
        free = (await db.keys_stats())["free_" + pool]
        need = amount * len(targets)
        label = POOL_LABEL[pool]
        if need > free:
            await i.followup.send(f"That needs **{need:,}** {label} codes but only **{free:,}** are in stock. "
                                  "Load more with `/importkeys` first.", ephemeral=True)
            return
        if role is None:
            owned = await db.keys_owned_count(user.id)
            extra = f"\nThey already hold **{owned:,}** codes. These would be **on top** of those." if owned else ""
            text = (f"Give **{amount:,}** {label} beta keys to {user.mention}? ({free:,} {label} in stock, "
                    f"{free - amount:,} would be left.){extra}")
        else:
            text = (f"Give **{amount:,}** {label} beta keys to **each** of the **{len(targets):,}** members with {role.mention}? "
                    f"That's **{need:,}** codes ({free:,} {label} in stock, {free - need:,} would be left).\n"
                    "People who already hold codes get these on top. Anyone with closed DMs gets them by running `/keys`.")
        await i.followup.send(text, view=_ConfirmGive(self, i.user.id, targets, amount, pool, role), ephemeral=True,
                              allowed_mentions=NO_MENTIONS)

    async def _give_one(self, guild: discord.Guild, target: discord.Member, amount: int, pool: str,
                        by: discord.abc.User, announce: bool):
        """Assign `amount` codes of `pool` to one member and DM them. Returns (claim result, dm_sent)."""
        res = await self.bot.db.keys_claim(target.id, amount, "grant", by.id, pool)
        if res.status != "ok":
            return res, False
        sent = False
        try:
            codes = await self._decrypt_async(res.codes)
            sent = await self._dm_codes(target, codes, GIVE_INTRO.format(n=amount))
        except vault.VaultError as e:
            log.error("givekeys: assigned but couldn't decrypt: %s", e)
        if announce:
            await self._after_claim(guild, target, amount, "grant", by, res.at, res.remaining, sent)
        return res, sent

    async def do_give(self, i: discord.Interaction, target: discord.Member, amount: int, pool: str = "regular"):
        """Runs after the admin pressed Confirm. `i` is the button press; the message is edited with the outcome."""
        try:
            res, sent = await self._give_one(i.guild, target, amount, pool, i.user, announce=True)
        except Exception:
            log.exception("givekeys: claim failed")
            await i.edit_original_response(content="⚠️ Something went wrong and **no keys were given**. "
                                                   "Check `/keystats` and try again.")
            return
        if res.status != "ok":      # someone else used codes while the admin was deciding
            await i.edit_original_response(content=f"Only **{res.remaining:,}** {POOL_LABEL[pool]} codes are left now, "
                                                   "so nothing was given.")
            return
        tail = ("They were sent to their DMs." if sent else
                f"I couldn't DM them, but the keys are saved to their account. Tell {target.mention} to run `/keys` "
                "and they'll get them in a private reply.")
        await i.edit_original_response(content=f"✅ Gave **{amount:,}** {POOL_LABEL[pool]} keys to {target.mention}. {tail}\n"
                                               f"-# {res.remaining:,} {POOL_LABEL[pool]} codes left in stock.",
                                       allowed_mentions=NO_MENTIONS)

    async def do_give_role(self, i: discord.Interaction, role: discord.Role, targets: list[discord.Member],
                           amount: int, pool: str):
        """Give `amount` codes to each member, one all-or-nothing claim per person. Stops if the stock runs out."""
        given = dm_failed = 0
        remaining = None
        stopped = False
        failed: list[discord.Member] = []
        for m in targets:
            try:
                res, sent = await self._give_one(i.guild, m, amount, pool, i.user, announce=False)
            except Exception:
                log.exception("givekeys(role): claim failed for %s", m.id)
                failed.append(m)
                continue
            if res.status != "ok":
                stopped = True
                break
            given += 1
            remaining = res.remaining
            dm_failed += 0 if sent else 1
            await asyncio.sleep(DM_PAUSE)
        label = POOL_LABEL[pool]
        ch = self._log_channel(i.guild)
        if ch is not None and given:
            e = discord.Embed(title="🔑 Beta keys given to a role", color=config.COLOR_ACCENT, timestamp=utcnow())
            e.add_field(name="Role", value=role.mention, inline=True)
            e.add_field(name="Members", value=f"**{given:,}** × **{amount:,}** {label} keys", inline=True)
            e.add_field(name="DMs closed", value=f"{dm_failed:,}", inline=True)
            e.add_field(name="By", value=f"{i.user.mention} with `/givekeys`", inline=False)
            e.set_footer(text=f"{remaining:,} {label} codes left in stock")
            try:
                await ch.send(embed=e, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                log.warning("Beta keys: couldn't post to the log channel %s", ch)
        lines = [f"✅ Gave **{amount:,}** {label} keys to **{given:,}** of {len(targets):,} members with {role.mention}."]
        if dm_failed:
            lines.append(f"{dm_failed:,} have closed DMs. The keys are saved on their accounts and they get them by running `/keys`.")
        if stopped:
            lines.append(f"⚠️ I ran out of {label} codes partway, so the rest got nothing. Load more with `/importkeys` and run it again for the others.")
        if failed:
            lines.append(f"⚠️ {len(failed):,} members hit an error and got nothing: " + " ".join(m.mention for m in failed[:20]))
        if remaining is not None:
            lines.append(f"-# {remaining:,} {label} codes left in stock.")
        await i.edit_original_response(content="\n".join(lines), allowed_mentions=NO_MENTIONS)
        if stopped and given < len(targets) and remaining is not None and remaining <= LOW_STOCK:
            await alerts.alert_staff(self.bot, i.guild, f"{label.capitalize()} beta keys are out or nearly out: **{remaining:,}** left.",
                                     key=f"beta-keys-low-{pool}")

    # ----------------------------------------------------------- /keystats
    @app_commands.command(name="keystats", description="How many beta keys are left and who has them (admins only)")
    @app_commands.guild_only()
    @app_commands.check(require_keys_manager)
    async def keystats(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        db = self.bot.db
        s, rows = await db.keys_stats(), await db.keys_claimants()

        e = discord.Embed(title="🔑 Beta key stats", color=config.COLOR_MAIN)
        e.add_field(name="Regular stock", value=f"**{s['free_regular']:,}** left of {s['total_regular']:,}", inline=True)
        e.add_field(name="Creator stock", value=f"**{s['free_creator']:,}** left of {s['total_creator']:,}", inline=True)
        e.add_field(name="Handed out (kept on record)", value=f"**{s['assigned']:,}**", inline=True)
        e.add_field(name="Testers (via /keys)", value=f"**{s['testers']:,}** people · {s['tester_keys']:,} keys", inline=True)
        e.add_field(name="Given (via /givekeys)", value=f"**{s['grantees']:,}** people · {s['grant_keys']:,} keys", inline=True)

        def who(r) -> str:
            parts = ([f"{r['tester']:,} tester"] if r["tester"] else []) + ([f"{r['granted']:,} given"] if r["granted"] else [])
            return f"<@{r['user_id']}> · **{' + '.join(parts)}** · <t:{int(r['last_at'].timestamp())}:R>"
        e.add_field(name=f"Latest claims (showing {min(len(rows), 10)} of {len(rows):,})",
                    value="\n".join(who(r) for r in rows[:10]) or "Nobody has claimed yet.", inline=False)

        problems = []
        if self._log_channel(i.guild) is None:
            problems.append("⚠️ No log channel is set, so claims are only saved in the database. Set `BETA_KEYS_LOG_CHANNEL_ID`.")
        if s["free_regular"] <= LOW_STOCK:
            problems.append(f"⚠️ Only {s['free_regular']:,} regular codes left.")
        if s["total_creator"] and s["free_creator"] <= LOW_STOCK:
            problems.append(f"⚠️ Only {s['free_creator']:,} creator codes left.")
        if problems:
            e.add_field(name="Heads up", value="\n".join(problems), inline=False)
        e.set_footer(text="The attached CSV lists everyone. It never contains codes.")

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["user_id", "name", "tester_keys", "given_keys", "total_keys", "of_which_creator_keys", "first_claimed_utc", "last_claimed_utc"])
        for r in rows:
            m = i.guild.get_member(r["user_id"])
            w.writerow([r["user_id"], csv_safe(m.display_name) if m else "(not in server)", r["tester"], r["granted"],
                        r["tester"] + r["granted"], r["creator"], r["first_at"].strftime("%Y-%m-%d %H:%M:%S"),
                        r["last_at"].strftime("%Y-%m-%d %H:%M:%S")])
        f = discord.File(io.BytesIO(out.getvalue().encode("utf-8-sig")), filename="beta-key-claims.csv")
        await i.followup.send(embed=e, file=f, ephemeral=True)

    # --------------------------------------------------------- /importkeys
    @app_commands.command(name="importkeys", description="Load the CSV of beta codes (admins only)")
    @app_commands.describe(file="The CSV from Banshee: one code per row (a header row is fine)",
                           type="Which stock these codes go into (default: regular)")
    @app_commands.choices(type=POOL_CHOICES)
    @app_commands.guild_only()
    @app_commands.check(require_keys_manager)
    async def importkeys(self, i: discord.Interaction, file: discord.Attachment,
                         type: Optional[app_commands.Choice[str]] = None):
        await i.response.defer(ephemeral=True)
        pool = type.value if type else "regular"
        if not await self._vault_ok(i, admin=True):
            return
        if not file.filename.lower().endswith((".csv", ".txt")):
            await i.followup.send("Please upload a `.csv` file (in Excel or Sheets: Save as / Download as CSV).", ephemeral=True)
            return
        if file.size > MAX_CSV_BYTES:
            await i.followup.send(f"That file is too big ({file.size / 1_048_576:.1f} MB, the limit is "
                                  f"{MAX_CSV_BYTES // 1_048_576} MB).", ephemeral=True)
            return
        try:
            parsed = parse_csv(await file.read())
        except KeysError as e:
            await i.followup.send(f"⚠️ {e}", ephemeral=True)
            return
        if not parsed.codes:
            await i.followup.send("I didn't find any codes in that file. Is the first column the code?", ephemeral=True)
            return

        pairs = await asyncio.to_thread(self._encrypt, parsed.codes)
        db = self.bot.db
        added = await db.keys_import(pairs, i.user.id, pool)
        stock = (await db.keys_stats())["free_" + pool]

        notes = [f"Read from column **{parsed.column + 1}**" + (" (header row skipped)." if parsed.header_found else ".")]
        if len(parsed.codes) - added:
            notes.append(f"**{len(parsed.codes) - added:,}** were already loaded before, so they were skipped.")
        for n, label in ((parsed.duplicates, "repeated in the file"), (parsed.invalid, "invalid (spaces inside or far too long)"),
                         (parsed.blank, "rows with an empty code")):
            if n:
                notes.append(f"Skipped **{n:,}** {label}.")
        await i.followup.send(
            f"✅ Loaded **{added:,}** new **{POOL_LABEL[pool]}** codes. **{stock:,}** {POOL_LABEL[pool]} codes are now in stock.\n"
            f"First code in the file looks like `{mask(parsed.codes[0])}`. If that's not a code, you picked the wrong column.\n"
            + "\n".join(f"• {n}" for n in notes)
            + "\n-# Codes are encrypted in the database. Delete the CSV from your own devices when you're done.",
            ephemeral=True)

    # --------------------------------------------------------- /resetkeys
    @app_commands.command(name="resetkeys", description="Delete the UNCLAIMED beta codes (claimed ones are kept) (admins only)")
    @app_commands.describe(type="Which stock to clear (default: both)")
    @app_commands.choices(type=POOL_CHOICES + [app_commands.Choice(name="Both", value="both")])
    @app_commands.guild_only()
    @app_commands.check(require_admin)
    async def resetkeys(self, i: discord.Interaction, type: Optional[app_commands.Choice[str]] = None):
        await i.response.defer(ephemeral=True)
        pool = type.value if type and type.value != "both" else None
        s = await self.bot.db.keys_stats()
        n = s["free"] if pool is None else s["free_" + pool]
        which = POOL_LABEL[pool] if pool else "regular and content creator"
        await i.followup.send(
            f"⚠️ This permanently deletes the **{n:,} unclaimed {which} codes** still in stock.\n"
            f"Codes that were already claimed or given ({s['assigned']:,}) and all claim records are **kept**. "
            "Nobody's claim status changes.\n**This can't be undone.** Are you sure?",
            view=_ConfirmReset(self, i.user.id, pool), ephemeral=True)


async def setup(bot):
    await bot.add_cog(BetaKeys(bot))
