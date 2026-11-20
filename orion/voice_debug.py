"""Temporary diagnostics for Discord's end-to-end voice encryption (DAVE / MLS) handshake.

discord.py hides every failure of that handshake: if it can't finish, the bot still "speaks" (green ring) but sends
unencrypted audio that nobody can hear. This module makes the handshake visible in the log. Remove the call to
enable() in main.py once audio works.

Only a whitelist of harmless handshake messages is shown from discord.gateway, because its other debug lines contain
the bot token and the voice secret key.
"""
import logging
import re
import traceback

log = logging.getLogger("orion.voicedebug")

_SAFE = re.compile(r"^(Voice websocket binary frame|Sending voice websocket binary frame|Set MLS|MLS |"
                   r"Preparing for DAVE|Executing DAVE|Voice RESUME|received supported|selected the voice)")


class _GatewayFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "discord.gateway" and record.levelno < logging.INFO:
            return bool(_SAFE.match(record.getMessage()))
        return True


def _wrap_recover() -> None:
    """discord.py swallows the exception behind a failed MLS commit/welcome. Log it before it is discarded."""
    from discord import voice_state
    cls = voice_state.VoiceConnectionState
    original = cls._recover_from_invalid_commit

    async def wrapped(self, transition_id):
        log.error("DAVE: invalid MLS commit/welcome for transition %s, cause:\n%s", transition_id,
                  traceback.format_exc())
        return await original(self, transition_id)

    cls._recover_from_invalid_commit = wrapped


def enable() -> None:
    for name in ("discord.voice_state", "discord.voice_client", "discord.gateway"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_GatewayFilter())
    try:
        _wrap_recover()
    except Exception as e:      # diagnostics must never stop the bot from starting
        log.warning("could not hook the DAVE error logging: %r", e)
