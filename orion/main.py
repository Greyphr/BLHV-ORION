import asyncio
import logging
import os
import signal
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands

from . import alerts, bot_rpc, config, health, voice_debug
from .db import Database
from .ui import PanelView

log = logging.getLogger("orion")


class OrionBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # privileged: enable in the dev portal
        intents.message_content = True  # privileged: enable in the dev portal
        intents.voice_states = True
        activity = discord.CustomActivity(name=config.BOT_STATUS) if config.BOT_STATUS else None
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                         activity=activity, status=discord.Status.online)
        self.db = Database(config.DATABASE_URL)
        self.failed_extensions: list[str] = []

    async def setup_hook(self):
        await self.db.connect()
        # One broken feature (for example music failing to import) must not take the whole bot down.
        for ext in ("orion.cogs.welcome", "orion.cogs.leveling", "orion.cogs.admin", "orion.cogs.announce",
                    "orion.cogs.giveaway", "orion.cogs.poll", "orion.cogs.ticket", "orion.cogs.twitch",
                    "orion.cogs.music", "orion.cogs.keys", "orion.cogs.dashboard", "orion.cogs.events",
                    "orion.cogs.profile", "orion.cogs.achievements", "orion.cogs.ai_admin",
                    "orion.cogs.moderation"):
            try:
                await self.load_extension(ext)
            except Exception:
                self.failed_extensions.append(ext)
                log.exception("Could not load %s. The bot is starting WITHOUT it.", ext)
        self.add_view(PanelView(self))

        @self.tree.error
        async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.CheckFailure):
                msg = str(error) or "You can't use that."
            else:
                log.exception("Command error", exc_info=error)
                msg = "Something went wrong. Please try again."
            send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            try:
                await send(msg, ephemeral=True)
            except discord.HTTPException:      # the interaction already expired: nothing left to tell them
                log.debug("Could not deliver the error message (interaction expired)")

        guild = discord.Object(config.GUILD_ID)   # guild sync = commands appear instantly
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if self.failed_extensions and not getattr(self, "_told_staff", False):
            self._told_staff = True
            guild = self.get_guild(config.GUILD_ID)
            if guild:
                await alerts.alert_staff(self, guild, "These features failed to load and are OFF: "
                                         + ", ".join(e.rsplit(".", 1)[-1] for e in self.failed_extensions)
                                         + ". Check the bot log.", key="failed-extensions")

    async def close(self):
        await self.db.close()
        await super().close()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.getenv("VOICE_DEBUG", "0") == "1":    # shows the voice encryption (DAVE) handshake; noisy, off by default
        voice_debug.enable()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            registered_signals.append(sig)
        except NotImplementedError:
            log.warning("Signal %s cannot be handled on this platform", sig)

    try:
        async with OrionBot() as bot:
            health_server = health.HealthServer(
                bot,
                host=os.getenv("HEALTH_HOST", "127.0.0.1"),
                port=int(os.getenv("HEALTH_PORT", "8080")),
            )
            await health_server.start()
            rpc_server = bot_rpc.BotRPCServer(
                bot,
                host=os.getenv("BOT_RPC_HOST", "0.0.0.0"),
                port=int(os.getenv("BOT_RPC_PORT", "8082")),
                token=os.getenv("BOT_RPC_TOKEN", ""),
            )
            await rpc_server.start()
            bot_task = asyncio.create_task(bot.start(config.TOKEN), name="orion-discord")
            stop_task = asyncio.create_task(stop.wait(), name="orion-shutdown")
            try:
                done, _ = await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                if bot_task in done:
                    await bot_task                       # surface a genuine startup/connection failure
                else:
                    log.info("Shutdown signal received; disconnecting Orion gracefully")
                    await bot.close()
                    with suppress(asyncio.CancelledError):
                        await bot_task
            finally:
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task
                await health_server.stop()
                await rpc_server.stop()
    finally:
        for sig in registered_signals:
            with suppress(NotImplementedError):
                loop.remove_signal_handler(sig)


if __name__ == "__main__":
    asyncio.run(main())
