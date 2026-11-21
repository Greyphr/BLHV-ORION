"""Authenticated internal RPC for the standalone Orion admin panel.

The admin panel never receives the Discord bot token. It calls this service over
the internal Docker network with a separate shared secret. Every action is
allowlisted and re-checks the acting Discord user against the bot's permission
helpers before touching Discord.
"""
from __future__ import annotations

import hmac
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable, Coroutine
from uuid import UUID

from aiohttp import web

from . import config as bot_config
from . import levels, roles
from .services import cultivation

log = logging.getLogger("orion.bot_rpc")

TOKEN_HEADER = "X-Orion-RPC-Token"


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return str(value)


def record(row: Any) -> dict[str, Any]:
    return jsonable(dict(row)) if row is not None else {}


def redact(value: Any) -> Any:
    """Remove credential-like fields before an action is written to the audit log."""
    sensitive = {"secret", "password", "token", "api_key", "client_secret"}
    if isinstance(value, dict):
        return {str(k): "[redacted]" if str(k).lower() in sensitive else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


NUMERIC_CONFIG_KEYS = frozenset({"dm_color", "ticket_color"})


def coerce_config_value(key: str, value: Any) -> Any:
    """Store the same types the Discord /admin editor stores (HTML inputs are strings)."""
    if key not in NUMERIC_CONFIG_KEYS:
        return value
    if value in (None, ""):
        return ""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number or blank") from exc


class BotRPCServer:
    def __init__(self, bot: Any, *, host: str = "0.0.0.0", port: int = 8082, token: str = "") -> None:
        self.bot = bot
        self.host = host
        self.port = port
        self.token = token
        self._runner: web.AppRunner | None = None
        self.actions: dict[str, tuple[str, Callable[..., Coroutine[Any, Any, Any]]]] = {
            "dashboard.snapshot": ("read", self._snapshot),
            "config.set_many": ("write", self._config_set_many),
            "xp.rules.reset": ("write", self._xp_rules_reset),
            "xp.adjust": ("write", self._xp_adjust),
            "xp.resync_roles": ("control", self._xp_resync_roles),
            "exclusions.set": ("write", self._exclusions_set),
            "tickets.setup": ("control", self._tickets_setup),
            "tickets.panel_refresh": ("write", self._tickets_panel_refresh),
            "tickets.types_reload": ("write", self._tickets_types_reload),
            "tickets.type_create": ("write", self._tickets_type_create),
            "tickets.type_update": ("write", self._tickets_type_update),
            "tickets.type_delete": ("write", self._tickets_type_delete),
            "tickets.banner_set": ("write", self._tickets_banner_set),
            "tickets.stats": ("read", self._tickets_stats),
            "tickets.blacklist_add": ("write", self._tickets_blacklist_add),
            "tickets.blacklist_remove": ("write", self._tickets_blacklist_remove),
            "giveaway.create": ("write", self._giveaway_create),
            "giveaway.end": ("control", self._giveaway_end),
            "giveaway.cancel": ("control", self._giveaway_cancel),
            "giveaway.reroll": ("control", self._giveaway_reroll),
            "poll.create": ("write", self._poll_create),
            "poll.finish": ("write", self._poll_finish),
            "poll.delete": ("control", self._poll_delete),
            "announcement.publish": ("write", self._announcement_publish),
            "announcement.schedule": ("write", self._announcement_schedule),
            "announcement.cancel_scheduled": ("write", self._announcement_cancel_scheduled),
            "announcement.template_save": ("write", self._announcement_template_save),
            "announcement.template_delete": ("write", self._announcement_template_delete),
            "ai.test": ("read", self._ai_test),
        }

    # ------------------------------------------------------------------ server
    def _authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get(TOKEN_HEADER, "")
        return bool(self.token) and hmac.compare_digest(supplied, self.token)

    def _ready(self) -> bool:
        pool = getattr(self.bot.db, "pool", None)
        return (
            self.bot.is_ready()
            and not self.bot.is_closed()
            and pool is not None
            and not getattr(pool, "_closed", False)
        )

    async def _healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok" if self._ready() else "unavailable", "ready": self._ready()})

    async def _capabilities(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized(reason="Invalid RPC token")
        return web.json_response(
            {"version": 1, "actions": [{"name": name, "level": level} for name, (level, _) in self.actions.items()]}
        )

    async def _dispatch(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized(reason="Invalid RPC token")
        if not self._ready():
            raise web.HTTPServiceUnavailable(reason="Bot is not ready")
        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(reason="Invalid JSON body") from exc
        name = str(body.get("action", ""))
        params = body.get("params") or {}
        actor_id = int(body.get("actor_id") or 0)
        if name not in self.actions:
            raise web.HTTPNotFound(reason=f"Unknown action: {name}")
        if not isinstance(params, dict):
            raise web.HTTPBadRequest(reason="params must be an object")

        level, handler = self.actions[name]
        member = await self._actor(actor_id)
        if member is None or not self._permitted(member, level):
            raise web.HTTPForbidden(reason="Acting Discord user lacks permission for this action")

        try:
            result = await handler(params, member, actor_id)
        except web.HTTPException:
            raise
        except Exception as exc:
            log.exception("RPC action %s failed", name)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        await self._audit(actor_id, name, params)
        return web.json_response({"ok": True, "action": name, "result": jsonable(result)})

    async def _actor(self, actor_id: int) -> Any:
        guild = self.bot.get_guild(bot_config.GUILD_ID)
        if guild is None or not actor_id:
            return None
        member = guild.get_member(actor_id)
        if member is None:
            try:
                member = await guild.fetch_member(actor_id)
            except Exception:
                return None
        return member

    def _permitted(self, member: Any, level: str) -> bool:
        from . import checks

        if str(member.id) == str(bot_config.OWNER_ID) or checks.is_admin(member):
            return True
        if level == "read":
            return checks.is_staff(member, self.bot.db)
        return False

    async def _audit(self, actor_id: int, name: str, params: dict[str, Any]) -> None:
        try:
            await self.bot.db.record_audit(actor_id, f"web.{name}", "admin", 0,
                                          params=jsonable(redact(params)))
        except Exception:
            log.warning("Could not record web audit for %s", name, exc_info=True)

    async def start(self) -> None:
        if not self.token:
            log.warning("BOT_RPC_TOKEN is not set; admin RPC is disabled")
            return
        app = web.Application()
        app.router.add_get("/rpc/health", self._healthz)
        app.router.add_get("/rpc/capabilities", self._capabilities)
        app.router.add_post("/rpc/action", self._dispatch)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("Admin RPC listening on http://%s:%s/rpc", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ----------------------------------------------------------------- helpers
    def _guild(self) -> Any:
        return self.bot.get_guild(bot_config.GUILD_ID)

    def _cog(self, name: str) -> Any:
        cog = self.bot.get_cog(name)
        if cog is not None:
            return cog
        # GroupCogs are registered under their group name ("ticket", "poll", ...),
        # which is lower-case even though the Python class is Tickets, Polls, etc.
        return next((c for n, c in self.bot.cogs.items() if n.lower() == name.lower()), None)

    def _channel(self, channel_id: int) -> Any:
        guild = self._guild()
        return guild.get_channel(int(channel_id)) if guild else None

    async def _refresh_panel(self) -> None:
        from .ui import refresh_panel

        await refresh_panel(self.bot)

    # ------------------------------------------------------------------ reads
    async def _snapshot(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        db = self.bot.db
        pool = db.pool
        counts: dict[str, Any] = {}
        for name, table in (
            ("users", "users"), ("tickets", "tickets"), ("giveaways", "giveaways"),
            ("polls", "polls"), ("announcements", "announcements"),
            ("scheduled", "scheduled_announcements"), ("ticket_types", "ticket_types"),
            ("mod_cases", "mod_cases"), ("events", "events"), ("achievements", "achievements"),
        ):
            counts[name] = await pool.fetchval(f"SELECT count(*) FROM {table}")
        config_rows = await pool.fetch("SELECT key, value FROM config ORDER BY key")
        return {
            "guild": {"id": self._guild().id, "name": self._guild().name},
            "counts": jsonable(counts),
            "giveaways": [record(row) for row in await db.gw_list(25)],
            "polls": [record(row) for row in await db.poll_list(25)],
            "tickets": [record(row) for row in await db.ticket_open_list(25)],
            "ticket_types": [record(row) for row in await db.tt_list()],
            "templates": list(await db.list_templates()),
            "scheduled": [record(row) for row in await db.announce_scheduled_list(15)],
            "blacklist": [record(row) for row in await db.bl_list(50)],
            "config": {row["key"]: jsonable(row["value"]) for row in config_rows},
            "exclusions": {
                "channels": sorted(db.excluded_channels),
                "roles": sorted(db.excluded_roles),
            },
            "ai_enabled": bool(db.get("ai_ticket_helper_enabled", False)),
        }

    # --------------------------------------------------------------- settings
    CONFIG_KEYS = frozenset({
        "welcome_enabled", "dm_enabled", "leave_enabled", "autorole_enabled",
        "xp_messages_enabled", "xp_voice_enabled", "levelup_announce_enabled",
        "music_enabled", "tickets_enabled", "ticket_dm_enabled",
        "rule_msg_min", "rule_msg_max", "rule_cooldown", "rule_daily_bonus",
        "rule_min_chars", "rule_voice_per_10min", "rule_voice_cap",
        "welcome_channel_id", "leave_channel_id", "levelup_channel_id", "alert_channel_id",
        "ticket_log_channel_id", "ticket_ping_role_ids", "ticket_ping_role_id",
        "staff_role_ids", "dm_title", "dm_text", "dm_footer", "dm_color",
        "dm_banner_position", "welcome_text", "welcome_layout", "leave_text",
        "ai_ticket_helper_enabled", "ticket_panel_title", "ticket_panel_text",
        "ticket_button_label", "ticket_color",
    })

    async def _config_set_many(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        values = params.get("values") or {}
        if not isinstance(values, dict) or not values:
            raise ValueError("values must be a non-empty object")
        changed = []
        xp_keys = False
        for key, value in values.items():
            if key not in self.CONFIG_KEYS:
                raise ValueError(f"Config key is not editable from the web panel: {key}")
            if key == "welcome_layout" and not isinstance(value, dict):
                raise ValueError("welcome_layout must be a JSON object")
            await self.bot.db.set(key, coerce_config_value(key, value))
            changed.append(key)
            xp_keys = xp_keys or key.startswith("rule_")
        if xp_keys:
            await self._refresh_panel()
        return {"changed": changed}

    async def _xp_rules_reset(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        await self.bot.db.reset_rules()
        await self._refresh_panel()
        return {"reset": True}

    async def _exclusions_set(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        kind = str(params.get("kind", ""))
        if kind not in ("channel", "role"):
            raise ValueError("kind must be channel or role")
        target_id = int(params["target_id"])
        excluded = bool(params.get("excluded", True))
        await self.bot.db.set_exclusion(kind, target_id, excluded)
        return {"kind": kind, "target_id": target_id, "excluded": excluded}

    # ---------------------------------------------------------------------- xp
    async def _xp_adjust(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        member_id = int(params["member_id"])
        mode = str(params.get("mode", "add"))
        amount = int(params.get("amount", 0))
        if mode not in ("add", "remove", "set", "reset"):
            raise ValueError("mode must be add, remove, set or reset")
        if mode == "reset":
            # reset_user() deletes the row, unlike adjust_xp() which only has add/remove/set.
            row = await self.bot.db.get_user(member_id)
            old_xp = int(row["xp"]) if row else 0
            await self.bot.db.reset_user(member_id)
            await self.bot.db.log_progression(
                member_id, old_xp, 0, levels.level_from_xp(old_xp), levels.level_from_xp(0), "admin", actor_id)
            change = cultivation.XpChange(member_id, old_xp, 0,
                                          levels.level_from_xp(old_xp), levels.level_from_xp(0))
        else:
            change = await cultivation.adjust_member_xp(self.bot.db, actor_id, member_id, mode, amount)
        target = self._guild().get_member(member_id)
        role_changed = False
        warning = None
        if target is not None:
            try:
                role_changed = await roles.sync_member_role(
                    target, levels.level_from_xp(change.new_xp), strict=True
                )
            except roles.RoleSyncError as exc:
                warning = str(exc)
        return {
            "member_id": member_id, "old_xp": change.old_xp, "new_xp": change.new_xp,
            "old_level": change.old_level, "new_level": change.new_level,
            "role_changed": role_changed, "warning": warning,
        }

    async def _xp_resync_roles(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        changed, note = await roles.resync_all(self._guild(), self.bot.db)
        return {"changed": changed, "note": note}

    # ----------------------------------------------------------------- tickets
    async def _tickets_setup(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("ticket")
        if cog is None:
            raise RuntimeError("Tickets cog is not loaded")
        return {"message": await cog.setup_tickets(self._guild())}

    async def _tickets_panel_refresh(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("ticket")
        if cog is None:
            raise RuntimeError("Tickets cog is not loaded")
        return {"message": await cog.refresh_panel() or "Panel refreshed."}

    async def _tickets_types_reload(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("ticket")
        if cog is None:
            raise RuntimeError("Tickets cog is not loaded")
        await cog.reload_types()
        note = await cog.refresh_panel()
        return {"reloaded": True, "note": note}

    async def _tickets_type_create(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        allowed = {"emoji", "description", "questions", "staff_role_ids", "category_id", "ping_role_ids"}
        fields = {k: v for k, v in params.items() if k in allowed and k != "name"}
        type_id = await self.bot.db.tt_create(name=str(params["name"]), **fields)
        await self._tickets_types_reload({}, member, actor_id)
        return {"type_id": type_id}

    async def _tickets_type_update(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        fields = dict(params)
        type_id = int(fields.pop("type_id"))
        await self.bot.db.tt_update(type_id, **fields)
        await self._tickets_types_reload({}, member, actor_id)
        return {"type_id": type_id}

    async def _tickets_type_delete(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        type_id = int(params["type_id"])
        await self.bot.db.tt_delete(type_id)
        await self._tickets_types_reload({}, member, actor_id)
        return {"type_id": type_id}

    async def _tickets_banner_set(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("ticket")
        if cog is None:
            raise RuntimeError("Tickets cog is not loaded")
        which = str(params.get("which", "panel"))
        if which not in ("panel", "ticket"):
            raise ValueError("which must be panel or ticket")
        message = await cog.set_banner(which, None, params.get("link"), bool(params.get("remove", False)))
        return {"message": message}

    async def _tickets_stats(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("ticket")
        if cog is None:
            raise RuntimeError("Tickets cog is not loaded")
        days = min(max(int(params.get("days", 30)), 1), 365)
        embed = await cog.stats(days)
        return {"embed": jsonable(embed.to_dict())}

    async def _tickets_blacklist_add(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        user_id = int(params["user_id"])
        await self.bot.db.bl_add(user_id, str(params.get("reason", "")), actor_id)
        return {"user_id": user_id}

    async def _tickets_blacklist_remove(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        user_id = int(params["user_id"])
        return {"removed": await self.bot.db.bl_remove(user_id)}

    # --------------------------------------------------------------- giveaways
    async def _giveaway_create(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        from .giveaway_ui import GiveawayDraft, RewardDraft

        cog = self._cog("giveaway")
        if cog is None:
            raise RuntimeError("Giveaways cog is not loaded")
        rewards = [RewardDraft(**reward) for reward in params.get("rewards", [])]
        payload = {k: v for k, v in params.items() if k not in ("rewards", "channel_id")}
        draft = GiveawayDraft(**payload)
        if not draft.title or not draft.prize_pool:
            raise ValueError("title and prize_pool are required")
        channel_id = int(params.get("channel_id") or self.bot.db.get("giveaway_channel_id") or 0)
        channel = self._channel(channel_id)
        if channel is None:
            raise ValueError("Giveaway channel not found")
        message, gid = await cog.publish(draft, member, channel.id)
        return {"giveaway_id": gid, "message_id": message.id, "jump_url": message.jump_url}

    async def _giveaway_end(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("giveaway")
        if cog is None:
            raise RuntimeError("Giveaways cog is not loaded")
        return {"ended": await cog.end_giveaway(int(params["giveaway_id"]), actor_id, early=True)}

    async def _giveaway_cancel(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("giveaway")
        if cog is None:
            raise RuntimeError("Giveaways cog is not loaded")
        return {"cancelled": await cog.cancel_giveaway(int(params["giveaway_id"]), actor_id)}

    async def _giveaway_reroll(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("giveaway")
        if cog is None:
            raise RuntimeError("Giveaways cog is not loaded")
        ok, message = await cog.reroll(int(params["giveaway_id"]), int(params["reward_id"]), actor_id)
        return {"rerolled": ok, "message": message}

    # -------------------------------------------------------------------- polls
    async def _poll_create(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("poll")
        if cog is None:
            raise RuntimeError("Polls cog is not loaded")
        channel = self._channel(int(params["channel_id"]))
        if channel is None:
            raise ValueError("Poll channel not found")
        message, poll_id, note = await cog.publish_poll(
            user=member,
            guild=self._guild(),
            channel=channel,
            question=str(params["question"]),
            options=list(params["options"]),
            private=bool(params.get("private", False)),
            seconds=int(params.get("seconds", 0) or 0),
            role_ids=list(params.get("role_ids", [])),
            votes_final=bool(params.get("votes_final", False)),
            banner_id=params.get("banner_id"),
        )
        return {"poll_id": poll_id, "message_id": message.id, "jump_url": message.jump_url, "note": note}

    async def _poll_finish(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("poll")
        if cog is None:
            raise RuntimeError("Polls cog is not loaded")
        return {"finished": await cog.finish(int(params["poll_id"]))}

    async def _poll_delete(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        cog = self._cog("poll")
        if cog is None:
            raise RuntimeError("Polls cog is not loaded")
        await cog.delete_poll(int(params["poll_id"]))
        return {"deleted": True}

    # ------------------------------------------------------------ announcements
    def _draft(self, params: dict[str, Any]) -> Any:
        from .cogs.announce import Draft

        return Draft.from_dict(params)

    async def _announcement_publish(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        from .cogs.announce import build_embeds, link_view, load_assets, mention_parts

        channel = self._channel(int(params["channel_id"]))
        if channel is None:
            raise ValueError("Announcement channel not found")
        draft = self._draft(params.get("draft") or {})
        files, thumb, banner, top = await load_assets(self.bot.db, draft)
        mentions = [m for m in (params.get("mentions") or []) if isinstance(m, dict)]
        ping_text, allowed = mention_parts(mentions, str(params.get("ping") or "none"))
        content = " ".join(text for text in (ping_text, params.get("message") or None) if text) or None
        embeds = build_embeds(draft, thumb, banner, top)
        view = link_view(draft)
        kwargs: dict[str, Any] = {"content": content, "embeds": embeds, "view": view,
                                  "allowed_mentions": allowed}
        if files:
            kwargs["files"] = files
        message = await channel.send(**kwargs)
        await self.bot.db.save_announcement(channel.id, message.id, actor_id, draft.title,
                                            draft.to_dict(), [], False)
        await self.bot.db.record_audit(actor_id, "announce.publish", "announcement", message.id,
                                      channel=channel.id, title=draft.title)
        return {"message_id": message.id, "jump_url": message.jump_url}

    async def _announcement_schedule(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        channel = self._channel(int(params["channel_id"]))
        if channel is None:
            raise ValueError("Announcement channel not found")
        draft = self._draft(params.get("draft") or {})
        publish_at = datetime.fromisoformat(str(params["publish_at"]))
        sid = await self.bot.db.announce_schedule_add(channel.id, actor_id, draft.to_dict(), [], False, publish_at)
        return {"schedule_id": sid}

    async def _announcement_cancel_scheduled(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        return {"cancelled": await self.bot.db.announce_cancel_scheduled(int(params["schedule_id"]))}

    async def _announcement_template_save(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        draft = self._draft(params.get("draft") or {})
        await self.bot.db.save_template(str(params["name"]), draft.to_dict(), actor_id)
        return {"saved": True}

    async def _announcement_template_delete(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        await self.bot.db.delete_template(str(params["name"]))
        return {"deleted": True}

    # ------------------------------------------------------------------------ ai
    async def _ai_test(self, params: dict[str, Any], member: Any, actor_id: int) -> dict[str, Any]:
        from . import ai

        if not ai.is_configured():
            raise RuntimeError("AI provider is not configured")
        prompt = str(params.get("prompt", "Reply with the single word: OK"))
        result = await ai.complete(
            "You are Orion's admin panel connection test. Follow the user's instruction exactly.",
            prompt,
            model=self.bot.db.get("ai_model", "") or None,
        )
        return {"response": str(result)[:2000]}
