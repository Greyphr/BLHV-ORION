import os
from pathlib import Path
from urllib.parse import quote


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


def _int_env(name: str, default: int = 0) -> int:
    """An integer setting. Unset, blank or not a number -> the default (never crashes the bot at import time)."""
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _id_list_env(name: str, default: str) -> tuple[int, ...]:
    """Comma-separated role IDs. Unset -> the default; set but blank -> none."""
    raw = os.getenv(name)
    raw = default if raw is None else raw
    return tuple(int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit())


def _database_url() -> str:
    """DATABASE_URL if given, else built from DB_HOST/DB_USER/DB_NAME/DB_PASSWORD with the password
    percent-encoded (a password containing @ / : # ? % would otherwise break the connection string)."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    pw = os.getenv("DB_PASSWORD")
    if not pw:
        raise RuntimeError("Missing required environment variable: DATABASE_URL (or DB_PASSWORD with DB_HOST)")
    return (f"postgresql://{quote(os.getenv('DB_USER', 'orion'), safe='')}:{quote(pw, safe='')}"
            f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'orion')}")


TOKEN = _req("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "1562411368458485820").strip() or "1562411368458485820")   # Blue Heaven
DATABASE_URL = _database_url()

# Twitch live announcements (optional: leave both empty and the /twitch commands just say it isn't set up).
# Create an app at https://dev.twitch.tv/console/apps (no Twitch login needed from streamers).
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_CHECK_SECONDS = 60       # how often Twitch is polled
TWITCH_PING_ROLE_ID = _int_env("TWITCH_PING_ROLE_ID", 0)   # starting role for the "now live" post (0 = none until /twitch ping sets one)
TWITCH_OFFLINE_GRACE = 3        # consecutive offline checks before a stream counts as ended (avoids re-posts on blips)

# Roles are matched by ID (a role can be renamed, or a look-alike created, without changing who has access).
# The *_NAME values are only a fallback for when the matching *_ID is not set, and are used in messages.
ADMIN_ROLE_ID = _int_env("ADMIN_ROLE_ID", 1570857006884065290)      # Sect Admin: full management access
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "Sect Admin")
# The Sect Patriarch (server owner account): always has full access, even if the admin role is changed.
OWNER_ID = _int_env("OWNER_ID", 985637303844225104)
# Always-on staff roles: all staff commands + they can work tickets. Extra ones can still be added in /admin.
# Default: the Staff role. (The Sect Admin role and the Patriarch are always staff too.)
STAFF_ROLE_IDS = _id_list_env("STAFF_ROLE_IDS", "1562412567547224125")
AFK_CHANNEL_NAME = os.getenv("AFK_CHANNEL_NAME", "Sleeping💤")

# ---- XP rules (from the levelling spec) ----
MSG_XP_MIN = 10
MSG_XP_MAX = 20
MSG_COOLDOWN = 60            # seconds, per user
VOICE_XP_PER_10MIN = 10      # paid out once per VOICE_BLOCK_MINUTES of eligible voice
VOICE_BLOCK_MINUTES = 10
VOICE_DAILY_CAP = 1200       # per user per day
# Special voice-XP perk: these members ignore VOICE_DAILY_CAP, count even when alone in a VC, and earn VOICE_PERK_BOOST extra
# (1.0 = +100%, so a 10 XP block pays 20 XP). Applies to voice XP only.
VOICE_PERK_USER_IDS: set[int] = set()   # members who ignore the voice cap / earn a boost (add IDs here)
VOICE_PERK_BOOST = 1.0
DAILY_BONUS = 150            # first eligible message of the day
MIN_MSG_CHARS = 4            # letters/digits left after stripping emoji, links, mentions

# ---- Branding ----
COLOR_MAIN = 0x1E3A8A        # deep celestial blue (embed side bar)
COLOR_ACCENT = 0x38BDF8      # sky blue
BRAND = "Blue Heaven"
BOT_STATUS = os.getenv("BOT_STATUS", "🔵Always forward. Always to win.")   # shown as the bot's status ("" = no status)

# ---- Giveaways ----
# Reward codes are encrypted before they touch the database. The key comes from GIVEAWAY_ENCRYPTION_KEY, or, if that is
# empty, from DATA_DIR/giveaway.key (created automatically on first use). BACK THAT FILE UP: without the key the stored
# codes can never be read again.
DATA_DIR = os.getenv("DATA_DIR", "data")
GIVEAWAY_ENCRYPTION_KEY = os.getenv("GIVEAWAY_ENCRYPTION_KEY", "").strip()
GIVEAWAY_WINNER_ROLE_NAME = os.getenv("GIVEAWAY_WINNER_ROLE_NAME", "🏆 Giveaway Winner")
GIVEAWAY_TICK_SECONDS = 20      # how often expired giveaways / claim deadlines / role retries are processed

# ---- Key distribution (beta / access keys). Leave the role IDs at 0 and /keys reports "not set up". ----
# /keys is for these roles (matched by ID). Testers get BETA_KEYS_PER_TESTER codes the first time; creators get whatever
# an admin sets with /givekeys. Claims are logged in BETA_KEYS_LOG_CHANNEL_ID (falls back to the staff alerts channel).
BETA_TESTER_ROLE_ID = _int_env("BETA_TESTER_ROLE_ID", 0)
BETA_CREATOR_ROLE_ID = _int_env("BETA_CREATOR_ROLE_ID", 0)
# Roles (besides Sect Admin) that may use /givekeys, /keystats, /importkeys and /resetkeys: staff and CORE STAFF.
BETA_KEYS_MANAGER_ROLE_IDS = _id_list_env("BETA_KEYS_MANAGER_ROLE_IDS", "")
BETA_KEYS_CHANNEL_ID = _int_env("BETA_KEYS_CHANNEL_ID", 0)   # Beta Testers may only run /keys in this channel (0 = anywhere)
BETA_KEYS_PER_TESTER = max(1, _int_env("BETA_KEYS_PER_TESTER", 3))
BETA_KEYS_LOG_CHANNEL_ID = _int_env("BETA_KEYS_LOG_CHANNEL_ID", 0)

# ---- Experimental: AI ticket handler (switch it on/off with /ai switch; tune it with /ai set) ----
# Any cloud LLM: AI_PROVIDER=gemini (default), anthropic, or openai (also any OpenAI-compatible service via AI_BASE_URL).
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").strip().lower() or "gemini"
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "").strip() or {"gemini": "gemini-2.5-flash", "anthropic": "claude-haiku-5-5"}.get(AI_PROVIDER, "gpt-4o-mini")
AI_BASE_URL = os.getenv("AI_BASE_URL", "").strip()

# ---- Music (ported from the Sarada bot) ----
MUSIC_MAX_PLAYLIST = 1000       # cap on tracks pulled from one playlist
MUSIC_IDLE_SECONDS = 300        # leave voice after this long with nothing playing
MUSIC_DEFAULT_VOLUME = 0.5
# Optional cookies.txt, only needed if YouTube starts refusing to play videos. A relative path is
# anchored to the project root (/app in Docker), e.g. COOKIES_FILE=data/cookies.txt
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
if COOKIES_FILE and not Path(COOKIES_FILE).is_absolute():
    COOKIES_FILE = str(Path(__file__).resolve().parent.parent / COOKIES_FILE)
# Run `pip install -U yt-dlp[default]` on every start (set AUTO_UPDATE_YTDLP=0 to turn it off)
AUTO_UPDATE_YTDLP = os.getenv("AUTO_UPDATE_YTDLP", "1").strip() != "0"
