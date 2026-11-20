"""Music: play YouTube/Spotify links, playlists and searches in a voice channel (ported from the Sarada bot).

/play <query>        play a YouTube or Spotify link, or search by name
/playlist <url>      queue a whole playlist or album
/skip /previous /pause /resume /stop /shuffle /volume /leave
/queue               what's playing and what's next
/nowplaying          post the now-playing panel again

Open to everyone, like the rest of the member commands. Staff can switch it off under /admin ->
Features. Needs ffmpeg and libopus (both are in the Dockerfile).
"""
import asyncio
import ctypes.util
import importlib.metadata
import importlib.util
import logging
import random
import shutil
import time
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

from .. import config, spotify
from ..music import (FFMPEG_BEFORE, Track, UserError, classify, fetch_playlist, fetch_video, find_on_youtube,
                     fmt_duration, list_id, resolve_track, search_first, spawn, update_ytdlp)

log = logging.getLogger("orion.music")

# Track titles and display names come from YouTube and Discord, so they're untrusted: a track
# called "@everyone" must not be able to ping the server.
NO_MENTIONS = discord.AllowedMentions.none()

LOOP_MODES = ("off", "track", "queue")
LOOP_LABELS = {"off": "Loop: off", "track": "Loop: song", "queue": "Loop: queue"}
QUICK_FAIL_SECONDS = 3        # a track that "ends" sooner than this without being skipped did not really play


def voice_problems() -> list[str]:
    """Things that make the bot join a channel but stay silent. Empty list = the voice setup looks fine."""
    problems = []
    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg is not installed")
    if not discord.opus.is_loaded():
        try:
            discord.opus.load_opus(ctypes.util.find_library("opus") or "libopus.so.0")
        except Exception:
            problems.append("libopus could not be loaded")
    if importlib.util.find_spec("davey") is None:
        problems.append("the `davey` package (Discord's voice encryption) is missing, so rebuild the image: "
                        "`docker compose build --no-cache bot`")
    else:
        try:
            ver = importlib.metadata.version("davey")
            if tuple(int(x) for x in ver.split(".")[:2] if x.isdigit()) >= (0, 2):
                problems.append(f"davey {ver} is too new for discord.py 2.7 (the voice encryption never finishes and "
                                "nobody hears anything). Rebuild with `docker compose build --no-cache bot`")
        except Exception:
            pass
    return problems


async def set_presence(bot: commands.Bot, title: str | None) -> None:
    """Show the current track as a listening status; fall back to Orion's normal status."""
    try:
        if title:
            activity = discord.Activity(type=discord.ActivityType.listening, name=title[:100])
        elif config.BOT_STATUS:
            activity = discord.CustomActivity(name=config.BOT_STATUS)
        else:
            activity = None
        await bot.change_presence(activity=activity)
    except discord.HTTPException as e:
        log.warning("could not set presence: %r", e)


# ───────────────────────────── per-guild player ──────────────────────────────
class GuildPlayer:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=50)
        self.current: Track | None = None
        self.vc: discord.VoiceClient | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.panel: discord.Message | None = None
        self.volume = config.MUSIC_DEFAULT_VOLUME
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._suppress_history = False
        self.loop_mode = "off"             # "off", "track" (repeat this song) or "queue" (recycle finished songs)
        self._skipped = False              # the track that just ended was skipped by someone
        self._halt = False                 # the track that just ended was stopped: never repeat it
        self._started = 0.0

    def is_active(self) -> bool:
        return bool(self.vc and (self.vc.is_playing() or self.vc.is_paused()))

    async def add(self, tracks: list[Track]):
        self._cancel_idle()
        was_active = self.is_active()
        self.queue.extend(tracks)
        await self.play_next()
        if was_active:
            await self.refresh_panel()

    async def play_next(self):
        async with self._lock:
            if self.is_active():
                return
            # whatever we are replacing counts as finished, unless a /previous
            # put it back on the queue on purpose.
            finished = self.current
            suppress, self._suppress_history = self._suppress_history, False
            skipped, self._skipped = self._skipped, False
            halted, self._halt = self._halt, False
            connected = bool(self.vc and self.vc.is_connected())
            if finished and not suppress and not halted and connected:
                quick_fail = not skipped and time.monotonic() - self._started < QUICK_FAIL_SECONDS
                if quick_fail:
                    # never loop a track that died right away, it would retry in a tight loop
                    log.warning("%s ended after under %ds without being skipped: the audio stream failed "
                                "(check the ffmpeg/yt-dlp lines above)", finished.title, QUICK_FAIL_SECONDS)
                    await self._say(f"**{finished.title}** stopped right after starting. The audio stream failed, "
                                    "so I can't repeat it.")
                elif self.loop_mode == "track" and not skipped:
                    self.queue.appendleft(finished)
                elif self.loop_mode == "queue":
                    self.queue.append(finished)
            while self.queue:
                if not self.vc or not self.vc.is_connected():
                    break
                track = self.queue.popleft()
                try:
                    stream = await asyncio.to_thread(resolve_track, track)
                except Exception as e:
                    log.warning("resolve failed for %s: %r", track.title, e, exc_info=True)
                    await self._say(f"Couldn't play **{track.title}**, skipping.")
                    continue
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(stream, before_options=FFMPEG_BEFORE, options="-vn"),
                    volume=self.volume,
                )
                loop = asyncio.get_running_loop()

                def after(err, loop=loop):
                    if err:
                        log.error("player error: %r", err)
                    # runs on the player thread, so hop back onto the event loop
                    loop.call_soon_threadsafe(lambda: spawn(self.play_next()))

                if finished and not suppress and track is not finished:      # a repeat isn't new history
                    self.history.append(finished)
                self.current = track
                log.info("Playing %s (%s)", track.title, track.url)
                self._started = time.monotonic()
                self.vc.play(source, after=after)
                await self._post_panel()
                await set_presence(self.bot, track.title)
                spawn(self._prefetch())
                spawn(self._log_voice_encryption())
                return
            # queue exhausted (or not connected)
            if finished and not suppress:
                self.history.append(finished)
            self.current = None
            await self._end_panel()
            await set_presence(self.bot, None)
            self.schedule_idle()

    async def _log_voice_encryption(self):
        """Diagnostic: Discord requires end-to-end (DAVE) encryption. If the session isn't ready, discord.py silently
        sends plain audio, the speaking ring still lights up, but nobody can hear it."""
        await asyncio.sleep(3)
        try:
            conn = getattr(self.vc, "_connection", None)
            version = getattr(conn, "dave_protocol_version", None)
            session = getattr(conn, "dave_session", None)
            ready = bool(getattr(session, "ready", False))
            can = getattr(conn, "can_encrypt", None)
            level = logging.INFO if can else logging.ERROR
            try:
                davey_ver = importlib.metadata.version("davey")
            except Exception:
                davey_ver = "?"
            log.log(level, "voice encryption check: davey=%s dave_protocol_version=%s session_exists=%s "
                           "session_ready=%s can_encrypt=%s%s", davey_ver, version, session is not None, ready, can,
                    "" if can else "  <-- audio is being sent UNENCRYPTED, so listeners will hear nothing")
        except Exception as e:
            log.warning("voice encryption check failed: %r", e)

    async def _prefetch(self):
        """Spotify tracks have to be matched on YouTube first. Do that for the next one while this one plays,
        so there's no gap between songs."""
        nxt = self.queue[0] if self.queue else None
        if not nxt or not nxt.search or nxt.yt_url:
            return
        try:
            await asyncio.to_thread(find_on_youtube, nxt)
        except Exception as e:
            log.debug("prefetch failed for %s: %r", nxt.title, e)     # play_next will try again and report it

    def previous(self) -> bool:
        if not self.vc:
            return False
        if self.history:
            prev = self.history.pop()
        elif self.current:
            prev = None                # nothing to go back to: restart this one
        else:
            return False
        if self.current:
            self.queue.appendleft(self.current)
        if prev:
            self.queue.appendleft(prev)
        self._suppress_history = True
        if self.is_active():
            self.vc.stop()            # triggers `after` -> play_next
        else:
            spawn(self.play_next())
        return True

    async def _idle_disconnect(self):
        await asyncio.sleep(config.MUSIC_IDLE_SECONDS)
        if not self.is_active() and self.vc and self.vc.is_connected():
            log.info("Idle for %ds, leaving %s", config.MUSIC_IDLE_SECONDS, self.vc.channel)
            await self.vc.disconnect()

    def _cancel_idle(self):
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None

    def schedule_idle(self):
        """Arm the idle disconnect, but only when there is genuinely nothing to wait for.

        A paused track counts as active, so pausing never starts the timer. Used at the end of
        the queue and when a load fails, since either way we end up connected with nothing playing.
        """
        self._cancel_idle()
        if self.is_active() or self.queue:
            return
        if self.vc and self.vc.is_connected():
            self._idle_task = spawn(self._idle_disconnect())

    async def _say(self, text: str):
        if self.text_channel:
            try:
                await self.text_channel.send(text, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    def skip(self):
        if self.vc and self.is_active():
            self._skipped = True
            self.vc.stop()          # triggers `after` -> play_next

    def stop_playback(self):
        """Clear the queue and stop for good: nothing gets repeated, whatever the loop mode."""
        self.clear()
        self._halt = True
        if self.vc and self.is_active():
            self.vc.stop()

    def cycle_loop(self) -> str:
        self.loop_mode = LOOP_MODES[(LOOP_MODES.index(self.loop_mode) + 1) % len(LOOP_MODES)]
        return self.loop_mode

    def clear(self):
        self.queue.clear()

    def shuffle(self):
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)

    def set_volume(self, value: float):
        self.volume = value
        if self.vc and isinstance(self.vc.source, discord.PCMVolumeTransformer):
            self.vc.source.volume = value

    def build_embed(self, paused: bool = False) -> discord.Embed:
        track = self.current
        embed = discord.Embed(title=track.title[:256] if track else "Nothing is playing", colour=config.COLOR_MAIN)
        if track:
            embed.url = track.url
        embed.set_author(name="Paused" if paused else "Now playing")
        bits = []
        if track and track.uploader:
            bits.append(track.uploader)
        if track and track.duration:
            bits.append(fmt_duration(track.duration))
        if bits:
            embed.description = " · ".join(bits)
        if track and (thumb := track.thumb):
            embed.set_image(url=thumb)
        embed.add_field(name="Up next", value=self.queue[0].title[:100] if self.queue else "nothing", inline=True)
        embed.add_field(name="In queue", value=str(len(self.queue)), inline=True)
        if self.loop_mode != "off":
            embed.add_field(name="Loop", value={"track": "this song", "queue": "whole queue"}[self.loop_mode], inline=True)
        if track and track.requester:
            embed.set_footer(text=f"Requested by {track.requester}")
        return embed

    async def _post_panel(self) -> bool:
        """Post a fresh panel, replacing the old one. False if we could not."""
        if not self.text_channel or not self.current:
            return False
        old = self.panel
        try:
            self.panel = await self.text_channel.send(embed=self.build_embed(), view=PlayerView(self))
        except discord.HTTPException as e:
            log.warning("could not post the now-playing panel: %r", e)
            return False
        if old:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        return True

    async def refresh_panel(self, new_buttons: bool = False):
        if not self.panel or not self.current:
            return
        paused = bool(self.vc and self.vc.is_paused())
        try:
            if new_buttons:      # e.g. /loop changed what the Loop button should say
                await self.panel.edit(embed=self.build_embed(paused=paused), view=PlayerView(self))
            else:                # only the embed is passed, so the buttons stay as they are
                await self.panel.edit(embed=self.build_embed(paused=paused))
        except discord.HTTPException as e:
            log.debug("could not refresh the now-playing panel: %r", e)

    async def _end_panel(self):
        if not self.panel:
            return
        try:
            await self.panel.edit(embed=discord.Embed(title="Queue finished.", colour=config.COLOR_MAIN), view=None)
        except discord.HTTPException as e:
            log.debug("could not clear the now-playing panel: %r", e)
        self.panel = None


async def require_same_channel(interaction: discord.Interaction, player: GuildPlayer) -> bool:
    """Block someone who isn't in the bot's voice channel from driving playback.

    If the bot isn't in voice at all there is nothing to protect, so the command runs and reports
    that there is nothing playing. /play and /playlist don't use this: they connect or move the
    bot through ensure_voice instead.
    """
    vc = player.vc
    if not isinstance(vc, discord.VoiceClient):
        return True
    user = interaction.user
    same_channel = (isinstance(user, discord.Member) and user.voice is not None
                    and user.voice.channel is not None and user.voice.channel == vc.channel)
    if not same_channel:
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return False
    return True


class PlayerView(discord.ui.View):
    """The transport controls that ride along with the now-playing embed."""

    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player
        self.sync_loop_button()          # a fresh panel shows the loop mode that is already on

    async def _guard(self, interaction: discord.Interaction) -> bool:
        return await require_same_channel(interaction, self.player)

    @discord.ui.button(emoji="⏮️", label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if self.player.previous():
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Nothing to go back to.", ephemeral=True)

    @discord.ui.button(emoji="⏯️", label="Play / Pause", style=discord.ButtonStyle.primary)
    async def toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        vc = self.player.vc
        if not isinstance(vc, discord.VoiceClient):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        paused = False
        if vc.is_playing():
            vc.pause()
            paused = True
        elif vc.is_paused():
            vc.resume()
        await interaction.response.edit_message(embed=self.player.build_embed(paused=paused), view=self)

    @discord.ui.button(emoji="⏭️", label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        self.player.skip()

    @discord.ui.button(emoji="🔁", label=LOOP_LABELS["off"], style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.player.cycle_loop()
        self.sync_loop_button()
        paused = bool(self.player.vc and self.player.vc.is_paused())
        await interaction.response.edit_message(embed=self.player.build_embed(paused=paused), view=self)

    def sync_loop_button(self):
        mode = self.player.loop_mode
        self.loop_button.label = LOOP_LABELS[mode]
        self.loop_button.style = discord.ButtonStyle.secondary if mode == "off" else discord.ButtonStyle.success

    @discord.ui.button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        self.player.stop_playback()


# ───────────────────────────── the cog ───────────────────────────────────────
class Music(commands.Cog, name="music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    async def cog_load(self):
        # Runs in the background so a slow or offline PyPI can never delay the bot coming online. yt-dlp is imported
        # lazily, so a track played before this finishes uses the installed version; the update applies from then on.
        self._update_task = asyncio.create_task(asyncio.to_thread(update_ytdlp))
        for problem in voice_problems():
            log.error("VOICE SETUP PROBLEM: %s. The bot can join a channel but will play no sound.", problem)

    async def cog_unload(self):
        task = getattr(self, "_update_task", None)
        if task and not task.done():
            task.cancel()
        for p in self.players.values():
            p._cancel_idle()
            if p.vc and p.vc.is_connected():
                await p.vc.disconnect(force=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.bot.db.get("music_enabled", True):
            raise app_commands.CheckFailure("Music is switched off. Staff can turn it on in `/admin` under Features.")
        return True

    # ------------------------------------------------------------- helpers
    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self.bot)
        return self.players[guild_id]

    def player_for(self, interaction: discord.Interaction) -> tuple[discord.Guild, GuildPlayer]:
        """Every command is guild_only, so a guild is always present at runtime."""
        guild = interaction.guild
        assert guild is not None, "guild_only command invoked outside a guild"
        return guild, self.get_player(guild.id)

    async def ensure_voice(self, interaction: discord.Interaction) -> GuildPlayer | None:
        user = interaction.user
        voice = user.voice if isinstance(user, discord.Member) else None
        voice_channel = voice.channel if voice else None
        if voice_channel is None:
            await interaction.followup.send("Join a voice channel first.")
            return None
        guild, player = self.player_for(interaction)
        perms = voice_channel.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            await interaction.followup.send(
                f"I need the **Connect** and **Speak** permissions in {voice_channel.name} to play anything there.",
                allowed_mentions=NO_MENTIONS)
            return None
        text = interaction.channel
        if isinstance(text, discord.abc.Messageable):
            player.text_channel = text
        vc = guild.voice_client
        if isinstance(vc, discord.VoiceClient):
            if vc.channel != voice_channel:
                # don't drag the bot out of a channel others are listening to
                if player.is_active():
                    where = vc.channel.name if vc.channel else "another channel"
                    await interaction.followup.send(f"I'm already playing in {where}.", allowed_mentions=NO_MENTIONS)
                    return None
                await vc.move_to(voice_channel)
                log.info("Moved to voice channel %s", voice_channel)
        else:
            # connect() is annotated as returning VoiceProtocol, but it always builds a VoiceClient
            vc = await voice_channel.connect()
            assert isinstance(vc, discord.VoiceClient)
            log.info("Joined voice channel %s", voice_channel)
        player.vc = vc
        return player

    async def load_and_queue(self, player: GuildPlayer, query: str, force_playlist: bool = False,
                             requester: str | None = None) -> str:
        """Resolve a link or search text and queue it. Returns a message for the user."""
        query = query.strip()
        if spotify.looks_like_spotify(query):
            return await self._load_spotify(player, query, requester)
        kind = classify(query)
        if kind == "unsupported":
            raise UserError("I only handle YouTube and Spotify links, or plain search text.")

        if force_playlist:
            lid = list_id(query) if kind != "search" else None
            if not lid:
                raise UserError("That link doesn't contain a playlist.")
            kind, query = "playlist", f"https://www.youtube.com/playlist?list={lid}"

        if kind == "playlist":
            title, tracks = await asyncio.to_thread(fetch_playlist, query)
            if not tracks:
                raise UserError("I couldn't find any playable tracks in that playlist.")
            for t in tracks:
                t.requester = requester
            await player.add(tracks)
            note = f" (capped at {config.MUSIC_MAX_PLAYLIST})" if len(tracks) >= config.MUSIC_MAX_PLAYLIST else ""
            log.info("Queued %d tracks from playlist %s", len(tracks), title)
            return f"Queued **{len(tracks)}** tracks from **{title}**{note}"

        if kind == "video":
            track = await asyncio.to_thread(fetch_video, query)
            track.requester = requester
            await player.add([track])
            log.info("Queued video %s (%s)", track.title, track.url)
            msg = f"Queued **{track.title}** [{fmt_duration(track.duration)}]"
            if list_id(query):
                msg += "\n(That link also had a playlist. Use `/playlist` to queue all of it.)"
            return msg

        track = await asyncio.to_thread(search_first, query)
        if not track:
            raise UserError("No results found.")
        track.requester = requester
        await player.add([track])
        log.info("Queued search result %s (%s)", track.title, track.url)
        return f"Queued **{track.title}** [{fmt_duration(track.duration)}]"

    async def _load_spotify(self, player: GuildPlayer, query: str, requester: str | None) -> str:
        """Queue a Spotify track, album or playlist. The audio itself is matched on YouTube when each song plays."""
        result = await spotify.load(query)
        for t in result.tracks:
            t.requester = requester
        await player.add(result.tracks)
        if result.kind == "track":
            t = result.tracks[0]
            return f"Queued **{t.title}** [{fmt_duration(t.duration)}] (from Spotify)"
        msg = f"Queued **{len(result.tracks)}** tracks from the Spotify {result.kind} **{result.title}**"
        if result.truncated:
            msg += f"\n(Spotify only lets me see the first {spotify.PLAYLIST_VISIBLE} tracks of a playlist.)"
        return msg

    async def _run_load(self, interaction: discord.Interaction, query: str, force_playlist: bool = False):
        await interaction.response.defer()
        player = await self.ensure_voice(interaction)
        if not player:
            return
        try:
            msg = await self.load_and_queue(player, query, force_playlist, interaction.user.display_name)
        except UserError as e:
            msg = str(e)
            # the bot is already connected but nothing will play: don't sit there until everyone leaves
            player.schedule_idle()
        except Exception as e:
            log.error("load error: %r", e, exc_info=True)
            msg = "Couldn't load that. It may be private, age-restricted, or blocked."
            player.schedule_idle()
        if problems := voice_problems():
            msg += "\n⚠️ **Voice setup problem, so you won't hear anything:** " + "; ".join(problems) + "."
        await interaction.followup.send(msg, allowed_mentions=NO_MENTIONS)

    # ------------------------------------------------------------ listeners
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        vc = member.guild.voice_client
        if vc and vc.channel and all(m.bot for m in vc.channel.members):
            log.info("Everyone left %s, disconnecting", vc.channel)
            self.get_player(member.guild.id).clear()
            await vc.disconnect()

    # ------------------------------------------------------------- commands
    @app_commands.command(name="play", description="Play a YouTube or Spotify link, or search by name")
    @app_commands.describe(query="YouTube link, Spotify track/album/playlist link, or search terms")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        await self._run_load(interaction, query)

    @app_commands.command(name="playlist",
                          description="Queue a whole YouTube or Spotify playlist/album")
    @app_commands.describe(url="YouTube playlist link, or a Spotify playlist/album link")
    @app_commands.guild_only()
    async def playlist(self, interaction: discord.Interaction, url: str):
        await self._run_load(interaction, url, force_playlist=True)

    @app_commands.command(name="skip", description="Skip the current track")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        _, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        if not p.is_active() or not p.current:
            return await interaction.response.send_message("Nothing is playing.")
        title = p.current.title
        p.skip()
        await interaction.response.send_message(f"Skipped **{title}**", allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="previous", description="Play the previous track again")
    @app_commands.guild_only()
    async def previous(self, interaction: discord.Interaction):
        _, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        if p.previous():
            await interaction.response.send_message("Going back.")
        else:
            await interaction.response.send_message("Nothing to go back to.")

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        guild, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        vc = guild.voice_client
        if isinstance(vc, discord.VoiceClient) and vc.is_playing():
            vc.pause()
            await p.refresh_panel()
            return await interaction.response.send_message("Paused.")
        await interaction.response.send_message("Nothing is playing.")

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        guild, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        vc = guild.voice_client
        if isinstance(vc, discord.VoiceClient) and vc.is_paused():
            vc.resume()
            await p.refresh_panel()
            return await interaction.response.send_message("Resumed.")
        await interaction.response.send_message("Nothing is paused.")

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        _, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        p.stop_playback()
        await interaction.response.send_message("Stopped and cleared the queue.")

    @app_commands.command(name="loop", description="Repeat the current song, the whole queue, or nothing")
    @app_commands.describe(mode="What to repeat")
    @app_commands.choices(mode=[app_commands.Choice(name="Off", value="off"),
                                app_commands.Choice(name="This song", value="track"),
                                app_commands.Choice(name="Whole queue", value="queue")])
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        _, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        p.loop_mode = mode.value
        await p.refresh_panel(new_buttons=True)
        await interaction.response.send_message(f"Loop: **{mode.name.lower()}**.")

    @app_commands.command(name="queue", description="Show what's playing and what's next")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        _, p = self.player_for(interaction)
        now = f"{p.current.title} [{fmt_duration(p.current.duration)}]" if p.current else "nothing"
        items = list(p.queue)
        lines = [f"{i + 1}. {t.title} [{fmt_duration(t.duration)}]" for i, t in enumerate(items[:10])]
        if len(items) > 10:
            lines.append(f"...and {len(items) - 10} more")
        await interaction.response.send_message(
            f"**Now playing:** {now}\n**Up next:**\n" + ("\n".join(lines) or "empty"),
            allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        _, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        p.shuffle()
        await p.refresh_panel()
        await interaction.response.send_message("Shuffled.")

    @app_commands.command(name="nowplaying", description="Show the now-playing panel again")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        _, p = self.player_for(interaction)
        if not p.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        if isinstance(interaction.channel, discord.abc.Messageable):
            p.text_channel = interaction.channel
        await interaction.response.defer(ephemeral=True)
        if await p._post_panel():
            await interaction.followup.send("Panel refreshed.", ephemeral=True)
        else:
            await interaction.followup.send("Couldn't post the panel.", ephemeral=True)

    @app_commands.command(name="volume", description="Set volume (0-100)")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
        _, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        p.set_volume(level / 100)
        await interaction.response.send_message(f"Volume set to {level}%")

    @app_commands.command(name="leave", description="Clear the queue and leave voice")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        guild, p = self.player_for(interaction)
        if not await require_same_channel(interaction, p):
            return
        p.clear()
        vc = guild.voice_client
        if isinstance(vc, discord.VoiceClient):
            await vc.disconnect()
        await interaction.response.send_message("Bye.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
