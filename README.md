# Orion: the Blue Heaven Sect bot

**Orion** is the official Discord bot of the **Blue Heaven Sect** (BLHV). One application for cultivation (XP and realms), welcome cards,
announcements, tickets, events, giveaways, polls, Twitch alerts and music, all configurable from the `/admin` panel.
It grew out of the Menace bot: everything useful was kept, rebuilt for Blue Heaven's realm system, and is being extended phase by phase
(see `CODEBASE_REVIEW.md` and `FUTURE_WEB_PLATFORM_TODO.md`).

Python 3.12, discord.py 2, PostgreSQL 16, Docker Compose.

**Access:** the Sect Patriarch (`OWNER_ID`) always has full access. The **Sect Admin** role (`ADMIN_ROLE_ID`) manages everything,
including `/xp`. The **Staff** role (`STAFF_ROLE_IDS`) gets the staff commands and can work tickets.

**Database:** schema changes are versioned SQL files in `orion/migrations/` and are applied automatically (once each) at startup.

## Contents

1. [Setup](#setup)
2. [Configuration](#configuration)
3. [Commands](#commands)
4. [The /admin panel](#the-admin-panel)
5. [Welcome card](#welcome-card)
6. [XP and levels](#xp-and-levels)
7. [Announcements](#announcements)
8. [Twitch live alerts](#twitch-live-alerts)
9. [Giveaways](#giveaways)
10. [Beta keys](#beta-keys)
11. [Polls](#polls)
12. [Tickets](#tickets)
13. [Music](#music)
14. [The build harness](#the-build-harness-build)
15. [AI providers and the switcher](#ai-providers-and-the-switcher)
16. [Maintenance](#maintenance)

## Setup

### Discord developer portal

1. Create an application and a bot, then copy the bot token.
2. On the Bot tab, turn on the Server Members and Message Content intents.
3. Invite the bot with this link (swap in your client ID). It includes Connect and Speak, which the music player needs:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=1099783466006&scope=bot%20applications.commands
   ```

4. In the server, drag the bot's role above the level roles (Earth King up to Orion). If it sits below them it can't hand them out.

### Running it

You need Docker Desktop. In the project folder:

```
copy .env.example .env        # then fill in DISCORD_TOKEN, GUILD_ID and DB_PASSWORD
docker compose up -d --build
docker compose logs -f bot
```

Once it's online, run `/setup` in Discord as an Sect Admin. It will:

* create the seven level roles
* create a Levelling category with `level-ups` and `rank-panel` channels and post the rank panel
* pick the welcome channel
* give Earth King to everyone who has no level role yet
* create a staff-only `orion-alerts` channel where the bot reports problems

The bot is only online while the PC and Docker Desktop are running. Turning on "Start Docker Desktop when you sign in" in Docker's
settings brings it back after a reboot. For real 24/7 hosting see [Moving to a server](#moving-to-a-server).

## Configuration

Settings live in `.env`. Only the first three are required.

| Variable | What it does |
|---|---|
| `DISCORD_TOKEN` | Bot token from the developer portal |
| `GUILD_ID` | Server ID (turn on Developer Mode, right-click the server icon, Copy Server ID) |
| `DB_PASSWORD` | Any long password, used by the bundled Postgres container |
| `ADMIN_ROLE_ID` / `ADMIN_ROLE_NAME` | Role with full access (Sect Admin). Matched by ID; the name is only a fallback if the ID is blank |
| `XP_ROLE_ID` / `XP_ROLE_NAME` | The only role allowed to use `/xp` (Blue Heaven). Matched by ID, name as fallback |
| `STAFF_ROLE_IDS` | Comma-separated always-on staff roles (CORE STAFF, Grand Marshals, staff). All staff commands + ticket handling. More can be added in `/admin` |
| `VOICE_DEBUG` | `1` turns on noisy voice-handshake logging. Default `0` |
| `AFK_CHANNEL_NAME` | Voice channel that never earns XP. Default `Sleeping💤` |
| `TIMEZONE` | Used for the daily reset and nightly checks. Default `Africa/Lagos` (WAT, UTC+1, no daylight saving). |
| `GIVEAWAY_ENCRYPTION_KEY` | Leave empty and the bot makes its own key in `data/giveaway.key`. See [Giveaways](#giveaways) |
| `GIVEAWAY_WINNER_ROLE_NAME` | Role given to giveaway winners. Default `🏆 Giveaway Winner` |
| `BETA_TESTER_ROLE_ID` / `BETA_CREATOR_ROLE_ID` | The Rivals Fantasy Beta Tester and content creator roles used by `/keys`. See [Beta keys](#beta-keys) |
| `BETA_KEYS_CHANNEL_ID` | The only channel where Beta Testers can run `/keys`. Default `1571489791772459048`; `0` = anywhere |
| `BETA_KEYS_MANAGER_ROLE_IDS` | Comma-separated roles (besides Sect Admin) that can use `/givekeys`, `/keystats` and `/importkeys` (`/resetkeys` is Sect Admin only). Default: staff and CORE STAFF |
| `BETA_KEYS_PER_TESTER` | Codes each Beta Tester gets the first time they run `/keys`. Default `3` |
| `BETA_KEYS_LOG_CHANNEL_ID` | Private staff channel where every key claim is logged. Blank = the staff alerts channel |
| `BOT_STATUS` | Status text under the bot's name |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Optional, turns on Twitch alerts. See [Twitch live alerts](#twitch-live-alerts) |
| `COOKIES_FILE` | Optional, only if YouTube starts refusing to play videos. See [Music](#music) |
| `AUTO_UPDATE_YTDLP` | Optional. Set to `0` to stop the bot upgrading yt-dlp on every start. Default `1` |
| `AI_API_KEY` | **Experimental.** Google Gemini API key (https://aistudio.google.com/apikey) for the AI ticket handler. Empty = the handler can't be turned on |
| `AI_FALLBACK_1_*` to `AI_FALLBACK_6_*` | Backup AI services, tried in order when the main one fails. See [AI providers and the switcher](#ai-providers-and-the-switcher) |
| `AI_PROVIDER` | `gemini` (default), `anthropic` or `openai` (also any OpenAI-compatible service with `AI_BASE_URL`) |
| `AI_MODEL` | Model name. Blank = `gemini-2.5-flash` (Gemini default) |
| `AI_BASE_URL` | Optional custom endpoint |
| `BUILDER_MODEL` | Model used by `/build` to write code (blank = `AI_MODEL`). Pick a strong coding model: Gemini Flash is fine for tickets but weak at writing cogs |

## Commands

| Who | Command |
|---|---|
| Everyone | `/rank [member]`, `/leaderboard`, `/levels`, plus the buttons on the rank panel |
| Everyone | `/play`, `/playlist`, `/skip`, `/previous`, `/pause`, `/resume`, `/stop`, `/queue`, `/shuffle`, `/volume`, `/nowplaying`, `/leave`. See [Music](#music) |
| Staff (Sect Admin and approved staff roles) | `/admin` |
| Staff | `/welcome banner`, `banner-reset`, `dm-banner`, `dm-banner-reset`, `channel`, `edit`, `toggle`, `preview`, `layout`, `layout-reset`, `settings` |
| Staff | `/announce new`, `edit`, `history`, `templates`, `template-delete` |
| Staff | `/twitch add`, `remove`, `list`, `channel`, `ping`, `preview` |
| Staff | `/giveaway create`, `manage`, `images` |
| Staff | `/poll create`, `manage`, `banner` |
| Beta Tester / content creator role | `/keys` |
| Sect Admin and the beta key manager roles | `/givekeys`, `/keystats`, `/importkeys`. See [Beta keys](#beta-keys) |
| Sect Admin only | `/resetkeys`. See [Beta keys](#beta-keys) |
| Everyone (in their own ticket) | `/ticket close`, plus the Close button on the ticket |
| Ticket staff (see [Tickets](#tickets)) | `/ticket claim`, `unclaim`, `transfer`, `add`, `remove`, `rename`, `priority`, `move`, `note`, `notes`, `reopen` |
| Staff | `/ticket panel`, `banner`, `stats`, `history`, `blacklist add`, `blacklist remove`, `blacklist list` |
| Sect Admin only | `/ticket setup` |
| Sect Admin / Patriarch only (Sect Admin can't use these) | `/realguys`, `/xp add`, `remove`, `set`, `reset`, `exclude-channel`, `include-channel`, `exclude-role`, `include-role`, `exclusions`, `resync-roles` |
| Sect Admin only | `/setup` |
| Sect Patriarch only (`OWNER_ID`, not even Sect Admin) | `/build new`, `modify`, `list`, `history`, `show`, `rollback`, `disable`, `enable`. See [The build harness](#the-build-harness-build) |

Staff roles are chosen in `/admin` under Staff access. If you want the admin commands hidden from everyone else, go to
Server Settings, Integrations, pick the bot, and restrict them there.

## The /admin panel

`/admin` opens a private panel with a dropdown at the top to switch sections. Anything you can set up through a slash command can be done
here with buttons instead.

| Section | What's in it |
|---|---|
| Overview | Features, XP rules, channels, staff roles, and how many giveaways and polls are running |
| Giveaways | New giveaway opens the builder, Manage giveaways opens the management panel. Lists the giveaways that are running |
| Polls | New poll opens the poll builder, Manage polls shows results, voters, banner, end now, export and delete. Lists open polls |
| Tickets | Run setup, refresh the panel, panel text and colour, settings (limits, auto-close, reopen window), ticket types, stats, blacklist, the log channel and the ping roles (up to 10). Lists open tickets |
| Ticket banner | Set, preview or remove the panel banner and the banner at the top of every ticket |
| Announcements | New announcement, History and Templates |
| XP & Levelling | Message and voice XP values, channels and roles that earn no XP, reset to defaults. Shows how long an active and a casual member would take to reach Orion |
| Features | Switch welcome, DM, leave message, auto role, message XP, voice XP, level-up posts, music, tickets and the ticket DM on or off |
| Welcome | Edit the welcome, DM and leave texts and preview the card |
| Welcome DM | Banner position, title, message, footer and colour of the DM new members get, with a preview |
| Channels | Welcome channel, level-up channel and the staff alerts channel |
| Staff access | Extra roles that may use the staff commands (Sect Admin only) |

The builders open as their own private message, so the panel stays where it is while you work.

The level curve and the seven rank roles are fixed on purpose and can't be edited from the panel.

## Welcome card

Run `/welcome banner` and attach your image. Don't paste a Discord link, those expire. You can also save the image as `assets/welcome_card.png`.

The banner should have the placeholders left blank, because the bot draws the avatar, username and member count on top. The banner that
ships with the bot (the 2170x725 WELCOME one) is prepared that way and the default layout is tuned for it: the avatar (with a white ring and a soft glow the bot draws)
sits in the purple panel on the right, the username in caps under WELCOME, and `MEMBER #n · BLUE HEAVEN SECT` below that. The fonts are Inter ExtraBold Italic
and Inter Bold Italic (`assets/fonts/Inter-*.otf`, SIL Open Font License).

The same banner is also built into the bot (`orion/defaults/welcome_card.png`), so if `assets/welcome_card.png` is ever missing or unreadable
(for example an empty `./assets` folder mounted by docker-compose) the card still uses the WELCOME banner. `/welcome banner-reset` goes back to it.

If you swap the banner, run `/welcome layout-reset` afterwards. Positions saved earlier with `/welcome layout` override the defaults
and will be tuned for the old image.

```
/welcome preview
/welcome layout element:avatar x:0.5 y:0.4 size:0.22
/welcome layout element:count count_format:"MEMBER #{count}"
```

Positions are fractions of the image (0 to 1), so any banner size works. Optional fonts: `assets/fonts/bold.ttf` and `regular.ttf`.

The Welcome DM has its own banner: `/welcome dm-banner` to set it, `/welcome dm-banner-reset` (or the panel button) to remove it.

To show a banner above the rank ladder, save it as `assets/level_roles_banner.png`. It appears on `/levels` and the Rank Ladder button.
Delete the file to go back to the plain ladder.

## XP and realms

Members cultivate through 10 realms with 9 sub-ranks each (90 ranks):
Earth King, Planetary, Stellar, Universe, Domain Lord, World Lord, Immortal, Universe Venerable, True God, Eternal True God.
Only the **realm** is a Discord role (it is swapped automatically on breakthrough); the sub-rank (e.g. *Earth King 5*) lives in the
database and shows on `/rank`, the leaderboard and level-up posts. There is no manual breakthrough step.

* Messages give 10 to 20 XP, once per 60 seconds per person. Messages under 4 letters or digits, links or emoji only, repeated text
  and `!command` style messages give nothing.
* The first eligible message of the day gets a +150 bonus. Days, weeks and months roll over on server time (`TIMEZONE`, default Africa/Lagos).
* Voice gives 10 XP for every 10 minutes in voice, up to 1,200 a day. It only counts with at least one other non-bot member present;
  muted, deafened and AFK-channel time does not count.
* The default curve is `floor(0.6·L² + 12·L + 100)` XP per step, 89 steps in total. It is a first draft: `levels.configure([...])`
  accepts any 89 step costs, and an admin screen for it is on the roadmap.
* Level-up posts go to **#realm-announcements** (`levelup_mode`: every sub-rank, or realm breakthroughs only).
  **#cultivation-logs** (staff only) records realm breakthroughs and every admin XP change. `/setup` creates the category and channels.
* Every rank change is stored in `progression_history`; every admin XP change also goes to `audit_log`.

The XP and rule values can all be changed in `/admin` under XP & Levelling.

### Banners
Level-up and welcome banners are drawn on `assets/levelup_card.png` and `assets/welcome_card.png` (2170x725). The bundled files are
placeholders generated by `python scripts/make_default_art.py`; replace them with real Blue Heaven artwork at any time. Optional
per-realm banners can be dropped into `assets/milestones/<realm name in lower case>.png` (for example `planetary.png` or `earth king.png`). Preview with
`python -m scripts.preview_cards`.

## Profiles and achievements

* `/profile [member]` shows realm, rank, Sect position, XP and progress to the next rank, join date, activity (XP-earning messages,
  voice hours, XP this week and month), achievements and recent events. When staff look at *another* member the reply is private and
  adds that member's recent progression history and user ID. `/rank` still shows the picture card.
* Achievements: `/achievement create | list | grant | revoke | disable`. Staff can list and grant; creating, revoking and disabling
  need the Sect Admin role. Each has a key, name, description, icon, category and rarity (common to legendary).
  Automatic unlock conditions and notifications are planned (Phase 7); for now they are granted by hand. Every grant is audited.
* Server join dates are copied from Discord into the database at startup and on the nightly check.

## Events, scrims and tournaments

`/event create`, `/scrim create`, `/tournament create` (staff) post a sign-up message with Join / Leave buttons, a slot limit and a waitlist
(the first waiting member is promoted and DMed when someone leaves). Sign-ups are stored in PostgreSQL and survive restarts, and
each member's event history shows on `/profile`. Attendance can be recorded in the database (`event_signups.attended`); commands to
mark attendance, cancel events and send reminders are planned (Phase 7).

## Moderation

Everything is recorded as a numbered **case** (warn, note, timeout, untimeout, kick, ban, unban), posted to a staff-only **mod-log** channel
and written to the audit log. Run **`/mod setup`** once to create the channel. The bot needs Kick Members, Ban Members, Moderate Members and
Manage Messages (all in the invite link in this README).

**Commands (staff):** `/warn`, `/note` (private, the member isn't told), `/timeout` (30m, 2h, 7d ... max 28d), `/untimeout`, `/case <number>`,
`/case-reason`, `/cases <user>`, `/reports`. **Sect Admin only by default:** `/kick`, `/ban` (permanent, or temporary with a duration, optional
message deletion for 0 to 7 days), `/unban <user ID>`, `/pardon <case>` (a pardoned warning stops counting). Temporary bans lift themselves, even
after a restart. **Members:** `/warnings` shows their own active warnings (without the moderator's name) and `/report` sends a private report
to the staff (rate-limited, no self-reports, no duplicates within 24 hours). Staff handle a report with the buttons under it in the mod log.
Members are DMed the reason and case number (`/mod set dm-members off` to stop that). Staff looking at a member with `/profile` also see
that member's moderation record.

**Safety rules for every action:** you can't act on yourself, the server owner / Sect Patriarch, other bots, or someone whose top role is equal to
or above yours. Staff can't act on staff or Sect Admins (only a Sect Admin can act on staff). The bot's own role must be above the target. If
Discord refuses (missing permission, role order), nothing is recorded and you are told why. A kick or ban DMs the member *before* acting,
because afterwards there is no shared server; if the action then fails, you are told they were already notified.

**Who may do what** is configurable: `/mod set admin-only ban,kick,unban,pardon` (list any of warn, note, timeout, untimeout, kick, ban, unban, pardon).

**Auto-moderation** (defaults are deliberately gentle: **alert only**, nothing is deleted or punished automatically):
* **Spam:** flags a message flood (default 6 messages in 5s), the same message repeated (4 copies in 30s) and mass mentions (6 or more in one
  message). Staff, bots, exempt roles and exempt channels are never checked (`/mod exempt`). `/mod set spam alert|act|off`. In `act` mode it
  deletes the flagged messages, times the member out (default 10 minutes), records a case and DMs them.
* **Raids:** alerts (pinging staff) when 8 members join within 30 seconds. `/mod set raid act` also times out the accounts in that burst that are
  younger than 7 days (default 60 minutes). Nobody is banned or kicked automatically.
* **Warning escalation:** off by default. `/mod set escalate-every 3` times a member out whenever they reach another 3 active warnings within
  30 days (`escalate-minutes`, `warn-window-days`).
* Tune with `/mod set` (`spam-rate 6/5`, `spam-duplicates 4/30`, `spam-mentions 6`, `raid-joins 8/30`, ...) and look at everything with `/mod status`.

## Announcements

`/announce new` opens a private builder with a live preview. From it you can:

* edit the title, description, footer, **author line** and colour (hex or the dropdown)
* add a thumbnail, a top banner and a bottom banner
* add up to five **link buttons** (under *Images & links*, one per line as `Label | https://link`)
* add or remove fields
* pick the channel and which roles or members to mention
* toggle the timestamp
* set a mass ping (off, @here or @everyone), which asks for a confirmation first
* save the draft as a template, or load one (see below)
* publish, or **schedule** it for later

**Templates:** eight come built in: `general-sect`, `community-update`, `esports`, `tournament-results`, `recruitment`, `community-event`,
`realm-milestone` and `maintenance`. Load one in the builder (or `/announce new template:...`) and change the text. Saving a template with
the same name overrides the built-in one; deleting your version brings the built-in back.

**Scheduling:** `/announce new when:"25/12 18:30"` (server time, Africa/Lagos by default) turns the Publish button into **Schedule**.
`/announce scheduled` lists what is waiting and `/announce unschedule <number>` cancels one. A background check runs every 30 seconds; it
retries on Discord outages, survives restarts, and tells the staff alerts channel if a scheduled post can't be sent (for example missing
permissions). The mass-ping permission is checked when you schedule, and the channel permissions again when it is sent.

**Edit and delete:** `/announce edit <message link>` reopens anything published through the bot and updates it in place;
`/announce delete <message link>` removes it. `/announce history` lists recent ones. Publishing, editing, scheduling and deleting are all
written to the audit log.

To attach images straight away: `/announce new top_banner:<image> banner:<image>`. Images are stored in the database when you add them,
so they keep working after Discord's temporary links expire.

The bot needs Mention Everyone (already in the invite link) to ping @here, @everyone and roles that aren't mentionable. The builder panel
stays open for 14 minutes. Unfinished drafts aren't kept, templates and scheduled announcements are.

## Twitch live alerts

The bot posts a "now live" message with a link, a ping and an embed (stream title, game, preview and the streamer's picture) when a
watched channel goes live.

1. Create an app at https://dev.twitch.tv/console/apps with the category Application Integration and the redirect URL `http://localhost`.
   Put the Client ID and Client Secret in `.env` as `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`, then run `docker compose up -d --build`.
   Streamers don't need to log in or authorise anything, only their username is used.
2. In the channel you want the posts in, run `/twitch channel`.
3. Add streamers with `/twitch add <username or twitch.tv link>`.

`/twitch ping` sets who gets pinged: a role (the default), @everyone, @here or nobody. Pick the role with `/twitch ping mode:Ping role role:@YourRole`; it is remembered. Until you pick one, the bot pings the role in `TWITCH_PING_ROLE_ID` (`1429172821522911232`). `/twitch preview` shows you a sample post that only you can see.
`/twitch list` shows who's being watched, with a red dot next to anyone who's live.

Twitch is checked once a minute. Each stream is announced once: restarts, short Twitch outages (three missed checks are tolerated)
and failed saves can't cause a second ping, but a new stream does get its own post. If the announcement channel is deleted or the bot
can't post in it, it says so in the staff alerts channel. In that channel the bot needs Send Messages and Embed Links, plus Mention Everyone
for the ping.

## Giveaways

### Creating one

`/giveaway create` (or New giveaway in `/admin`) opens a private builder. Under the controls there's a live preview of the real post: a
card with a banner on top, your text, a stats line, an optional bottom banner and the buttons inside the card. Nothing is posted until you
press Publish.

Choose the channel, the check-in interval (6h, 12h, 24h or custom) and the roles allowed to enter, then use the buttons:

* Text: the title and one big text box, pre-filled with a template
* Settings: number of winners, duration, claim deadline
* Rewards: one slot per prize, for example 1st: $500, 2nd: $300
* Images: top banner, bottom banner, small side image
* Style: card colour, button names and emojis

Images can be uploaded with the options on `/giveaway create`, which is the better way, or pasted as links. `/giveaway images` swaps them
on a giveaway that's already live.

### Card text

The card text is a single box that takes Discord markdown: `## Big heading`, `-# small grey text`, `- bullets`, bold, emojis.
Placeholders get filled in and stay live while the giveaway runs:

```
{title} {prize} {rewards} {winners} {ends} {ends_date} {starts_date}
{interval} {participants} {entries} {roles} {id}
```

The Placeholders button in the builder shows the list. Dates appear in each member's own timezone. The text is limited to about 3,600
characters once the placeholders are filled in, and the builder tells you if you go over.

### Rewards

Reward types are gift card, cash, game key, subscription and digital product (you enter the codes), plus physical or custom (you enter
delivery instructions). Codes are encrypted before they are stored. They never show up in the public post, the results message, exports
or the audit log.

### What members do

Members press the buttons on the giveaway post.

* Enter / Check in needs at least one of the eligible roles. By default that's Nuisance, Threat, Disaster, Calamity, Catastrophe or Orion,
  and you can change the list per giveaway. After joining they come back every check-in interval for one more entry. Only one entry per
  interval counts, and entries already earned are kept if they miss one.
* My entries shows their entries, the time left, the number of participants and when the next check-in opens.
* Anyone without an eligible role who presses Enter gets a private message listing the roles they need.

### When the timer runs out

The post turns into an ENDED card. Your text stays and the winners and a Claim Reward button are added. The bot then:

1. closes entries and draws the winners, weighted by entries with a secure random source (nobody wins two slots unless you allow it)
2. gives each winner the Giveaway Winner role, creating it if it doesn't exist
3. posts the results with @mentions and the Claim Reward button, and updates the original post

Winners press Claim Reward to get their code, shown once in a private message with a copy sent by DM. Everyone else gets a friendly
"not selected" message.

Timers are stored timestamps, not in-memory countdowns. Giveaways survive restarts, and one that expired while the bot was offline is
finished when it comes back.

### Managing one

`/giveaway manage` opens the management panel. From there you can edit the text and banner, extend the timer, end early, cancel,
see participants and entries per person, see winners (mark as delivered, or re-open a claim if someone lost their code), reroll, manage
rewards, change eligible roles and the winner role, set the claim deadline and automatic re-assignment of unclaimed prizes, read the
audit log, and export participants, the entry ledger, winners and the draw record as CSV or JSON. Every action goes in the audit log.

### The encryption key

On first use the bot creates `data/giveaway.key`. The automatic backups only contain the database, so copy that key file somewhere safe as
well. Without the key, the stored codes can't be recovered even if you have the database. You can set `GIVEAWAY_ENCRYPTION_KEY` in `.env`
instead if you'd rather manage it yourself. The bot refuses to touch stored rewards if it starts with a different key.

## Beta keys

One-use Rivals Fantasy beta codes, handed out by the bot so nobody has to be DMed by hand. Each code only ever goes to one person.

### Loading the codes

Banshee sends a CSV with one code per row (a header row such as `code` is fine; if there are several columns, the one headed `code` or
`key` is used, otherwise the first). An Sect Admin runs `/importkeys` and attaches the file. The bot replies privately with how many were
loaded and what the first code looks like with most of it hidden, so you can check it read the right column. Loading the same file
twice does nothing, and a code that's already stored is never loaded again. Run it again whenever Banshee sends more.

### What members do

- **`/keys`** (Beta Tester role). The first time, the bot assigns the tester 3 codes (`BETA_KEYS_PER_TESTER`) and DMs them. Running it
  again only shows the **same** codes, in a reply nobody else can see. Never new ones. If their DMs are closed, the codes appear in that
  private reply instead. Codes are never posted in a channel.
- Members who were given codes with `/givekeys` (for example creators) can also run `/keys` to see them again.
- A member with no codes and no role is told keys are for Beta Testers. A creator who hasn't been given a batch yet is told to ask staff.

### What admins do

- **`/givekeys amount`** with a `user` **or** a `role`, and an optional `type`: **Regular keys** (the default) or **Content creator keys**.
  A `user` gets `amount` codes. A `role` gives `amount` codes to **each** member with it, so 10 to a role of 30 people uses 300 codes. You
  get a confirm button first showing the total, so a typo can't hand out thousands. Batches of more than 25 codes are delivered as a
  `.txt` file. If the DMs are closed the codes stay saved on their account and they get them by running `/keys`. You never see the codes.
  It can also be used on someone who already has codes: the new ones are added on top. A role is capped at 500 members per go, and
  bots are skipped. If the stock runs out partway through a role, the rest get nothing and you're told, so you can load more and run it again.
- **`/keystats`** shows codes left, how many were handed out, how many testers claimed and how many were given, the latest claims, and
  attaches a CSV of everyone (name, tester keys, given keys, dates). It never contains codes.
- **`/importkeys file type`** loads a CSV into the **regular** or the **content creator** stock (`type`, default regular). The two stocks are kept apart, so creator codes are only ever given out with `/givekeys type:Content creator keys`, and testers only ever get regular ones.
- **`/resetkeys [type]`** (Sect Admin only) deletes the **unclaimed** codes still in stock (regular, creator or both). Codes that were already claimed or given, and every claim record, are **kept**, so nobody's status changes and the same code can't be handed out again. It shows how many will be deleted and asks you to confirm, and posts a note in the keys log channel.

### The claim log

Every time a Beta Tester runs `/keys` (first claim, looking at their keys again, hitting an empty stock, or trying it in the wrong channel) the bot also posts who it was, the result and the channel in the staff **alerts channel**, never any codes.

Every claim and grant is saved permanently (who, how many, when, and which admin for `/givekeys`) and posted in the private staff
channel set by `BETA_KEYS_LOG_CHANNEL_ID`. If you leave that blank it goes to the staff alerts channel. The log never contains a code.
Staff are also alerted when 100 or fewer codes are left or when someone can't get theirs because stock ran out. Nobody loses their place
when that happens, they can run `/keys` again after more codes are loaded.

### Safety

Codes are encrypted before they reach the database, using the same key as giveaway rewards (see [The encryption key](#the-encryption-key)),
so back up `data/giveaway.key` as well. The bot refuses to hand out codes if started with a different key. Two people pressing at the same
moment can't get the same code, and a claim either gives the full amount or nothing at all.

## Polls

A poll is a message with one button per option, 2 to 10 of them. A member gets one vote, and unless the poll was made with final votes
they can change it by pressing another button.

### Public and private

* Public polls show the live tally, and a Who voted button lets anyone see who picked what.
* Private polls only show totals. Members can't see who voted. Staff can, in the manage panel, which lists the voters under each option.

Private doesn't mean the vote is stored without a name. The voter's ID is saved either way, because that is how double voting is blocked.
It just means members can't see it. Staff and anyone with database access still can.

### Creating a poll

Either use New poll under Polls in `/admin`, which opens a builder with buttons and a live preview, or use the command:

```
/poll create question:"Which day for the tournament?" options:"Friday | Saturday | Sunday" duration:2d
```

Type the options separated by `|`, and choose Public or Private from the visibility dropdown. The rest is optional:

| Setting | What it does |
|---|---|
| `duration` | How long it runs, for example `2h` or `1d 12h`, from 1 minute to 30 days. Leave it empty and the poll stays open until staff close it |
| `channel` | Where to post it. Defaults to the channel you ran the command in |
| `role`, `role2` | Only members with one of these roles can vote. Empty means everyone |
| `votes_final` | Members can't change their vote once cast |
| `banner` or `banner_url` | An image shown under the results, uploaded or from a link |

`/poll banner` adds, replaces or removes the banner on a poll that's already live. The poll builder in `/admin` takes the banner as a link
only, because Discord forms can't take file uploads.

### Managing polls

`/poll manage` (or Manage polls in `/admin`) shows the results with every voter, and has buttons for Banner, End now, Export CSV
(every vote with the voter) and Delete. When a poll ends, the message switches to final results with the winning option or options
marked, and the buttons are switched off.

Poll buttons keep working after a restart. A poll whose timer ran out while the bot was offline is closed on the next start.

## Tickets

Members open a private ticket from a panel. Each ticket is its own channel, such as `ticket-0042-greyphr`, that only the member and the
staff for that ticket type can see.

### Setting it up

Run `/ticket setup` as an Sect Admin, or press Run setup under Tickets in `/admin`. It creates:

* a **🎫・Tickets** category, hidden from everyone except staff
* **📩・open-a-ticket**, a read-only channel with the ticket panel
* **📜・ticket-logs**, a staff-only channel for transcripts and ratings
* four ticket types: Support, Report a player, Partnership and Giveaway prize claim

Running it again only creates what's missing. To put the panel somewhere else, use `/ticket panel channel:#somewhere`, which
removes the old panel.

### Ticket types

Open Ticket types under Tickets in `/admin`. For each type you can set:

| Setting | What it does |
|---|---|
| Name, emoji, description | What members see in the panel dropdown |
| Form questions | Up to 5 questions asked when the ticket opens. The answers are shown at the top of the ticket. No questions means the ticket opens straight away |
| Staff roles | Who handles this type. Empty means Sect Admin plus the staff roles from Staff access. Sect Admin always has access |
| Category | Where these tickets are created. Empty means the shared Tickets category. **Own category** creates one for this type |
| Ping roles | Up to 10 roles pinged when a ticket of this type opens. Empty means the ping roles set under Tickets |
| Turn off / on | Hide the type from the panel without deleting it |

A category holds at most 50 channels. If one fills up, new tickets fail and the bot posts a staff alert. Give busy types their own
category, or lower the reopen window so closed tickets are deleted sooner. Changing a type's staff roles applies to new tickets only.

### What members do

Pick a type on the panel (or press the button when there is only one type), fill in the short form, and the bot links the new
channel. Members can have one open ticket at a time by default (change this under Settings). The Close button and `/ticket close`
work for the member who opened the ticket and for staff.

### What staff do

The message at the top of each ticket has three buttons:

* **Close** asks for confirmation, with an optional reason.
* **Claim** marks you as the person handling it. The top message shows who that is.
* **Staff tools** opens a private panel: priority (shown on the top message), move to another type, add or remove a member, transfer
  to another staff member, rename, staff notes and unclaim.

All of these also exist as `/ticket` commands. Discord allows only 2 renames per channel every 10 minutes, so a rename may ask you to
wait.

**Staff notes** are stored in the database only. They are never posted in the ticket and never appear in the transcript.

### Closing, transcripts and reopening

When a ticket closes:

1. The member, and anyone added to the ticket, lose access. Staff keep it.
2. An HTML transcript of the whole conversation goes to the log channel with a summary (who opened, handled and closed it, how long
   the first staff reply took, the reason). Open the file in any browser. Attachment links inside it come from Discord and stop
   working after a while.
3. The member gets the transcript by DM with 1 to 5 star buttons and an optional comment. Ratings are posted in the log channel.
   Switch the DM off under Features.
4. A Reopen / Transcript / Delete now message appears in the channel. The member or staff can reopen the ticket until the reopen window ends
   (24 hours by default), then the channel is deleted. The transcript stays in the log channel.

### Auto-close

A ticket where nobody has written for 72 hours gets a warning. If nobody replies within the next 24 hours, it closes with the reason
"No reply". Both times can be changed under Settings, and setting the first to 0 turns auto-close off.

### Banners

There are two banners: the **panel banner** on the message where members open tickets, and the **ticket banner** at the top of every
new ticket. Upload one with `/ticket banner` (pick the target, attach the image), or set one from a link under Ticket banner in
`/admin`, where you can also preview and remove them. The panel text, button label and colour are under Tickets → Panel text.

### Stats, history and the blacklist

* `/ticket stats` (or Stats in `/admin`) shows tickets opened and closed, the average first staff reply, the average rating, and the
  same per staff member.
* `/ticket history member:@someone` lists a member's tickets with links to their transcripts.
* `/ticket blacklist add` stops a member from opening tickets. Their open tickets stay open.

## Music

Plays YouTube videos, playlists, Spotify links and plain-text searches in a voice channel. Join a voice channel first, the bot connects to whichever one
you're in. Anyone can use it. Staff can switch it off under Features in `/admin`.

| Command | Description |
|---|---|
| `/play <query>` | Play a YouTube or Spotify link, or search by name. A video link that also carries a `list=` queues just that video |
| `/playlist <url>` | Queue a whole YouTube playlist (also works on a video link that includes a list) or a Spotify playlist / album. Capped at 1000 tracks |
| `/skip`, `/previous` | Skip the current track, or go back one |
| `/pause`, `/resume` | Pause and resume |
| `/stop` | Stop and clear the queue |
| `/loop <off\|this song\|whole queue>` | Repeat the current song, or recycle the whole queue. The panel's Loop button cycles the same three modes |
| `/queue` | What's playing and the next 10 tracks |
| `/shuffle` | Randomise the queue |
| `/volume <0-100>` | Set the volume |
| `/nowplaying` | Post the now-playing panel again |
| `/leave` | Clear the queue and leave voice |

Whenever a track starts the bot posts a panel in the channel it was queued from: artwork, title, uploader, length, who asked, what's next and how
much is left. It replaces the previous panel each time and carries Previous, Play / Pause, Next and Stop buttons. The buttons and the
control commands only work for someone in the same voice channel as the bot. Finished and skipped tracks go into a 50-track history that
`/previous` draws on; with nothing in it, `/previous` restarts the current track. While something plays, the bot's status shows it as
"Listening to ..." and goes back to the normal status afterwards.

The bot leaves on its own after 5 minutes with nothing playing, and straight away when everyone else leaves the channel. Track titles can't
ping anyone. The queue is kept in memory, so a restart empties it.

**Spotify.** Paste a Spotify track, album or playlist link (`open.spotify.com/...`, a `spotify.link` share link or a `spotify:track:...` URI) into
`/play` or `/playlist`. Spotify's audio is DRM protected and can't be streamed by any bot, so the bot reads the song names and artists from
Spotify's public embed page and plays the best match from YouTube. That needs no Spotify account, API keys or setup. Each song is matched
right before it plays (the next one is looked up while the current one plays), so a long playlist queues instantly. Because it is a
search, the occasional song can match a live or cover version. Spotify only exposes the first 100 tracks of a playlist, and artist, podcast
and "Liked Songs" links aren't supported. If Spotify ever changes that page, Spotify links will say they can't be read, while YouTube keeps working.

**No sound?** If the bot joins and shows a panel but you hear nothing, the cause is almost always the voice setup. The bot checks it on start and
says so in chat (and logs `VOICE SETUP PROBLEM`). The usual fix is `docker compose build --no-cache bot && docker compose up -d`, because voice needs
`discord.py` 2.7+ with its `davey` package (Discord's voice encryption), plus ffmpeg and libopus (both already in the image). Also check that the bot
has **Connect** and **Speak** in that voice channel and isn't server-muted.

**yt-dlp.** YouTube changes often and an outdated yt-dlp is the most common cause of "Couldn't load that". The bot runs
`pip install -U "yt-dlp[default]"` on every start (a few seconds, never fatal). Set `AUTO_UPDATE_YTDLP=0` to turn that off. The image also
contains ffmpeg and Deno, which yt-dlp needs to solve YouTube's challenges.

**If every video still fails**, export your YouTube cookies in Netscape format, save them as `data/cookies.txt`, set `COOKIES_FILE=data/cookies.txt`
in `.env` and run `docker compose up -d --build`. A cookie file is a login session, so keep it private. `*cookies*` is already in `.gitignore`.

## The build harness (/build)

Sect Patriarch only. Describe a feature in plain words and the AI writes it as a new cog file. You read the code in Discord, approve
it, and the bot loads it live, with no rebuild. If anything breaks, the previous version is put back. Needs `AI_API_KEY`
(same provider settings as the ticket AI; set `BUILDER_MODEL` to a strong coding model).

```
/build new request: Make a #choose-your-role channel with a role menu. Create one role per Marvel Rivals ranked tier.
```

1. The AI writes the feature. A private message shows its summary, the assumptions you should check, any new database tables, and the
   **code as an attached file**.
2. **Approve & load** loads it into the running bot. **Request changes** opens a box where you tell the AI what to fix; you get a new
   version to review. **Reject** throws it away. Nothing runs until you press Approve.
3. The buttons keep working after a restart. `/build show build:<number>` brings a draft back.

| Command | What it does |
|---|---|
| `/build new` | The AI writes a new feature for you to review |
| `/build modify feature request` | The AI changes an existing feature (it is given the current code). A draft that was waiting is replaced |
| `/build list` | Features, whether they're running, and drafts waiting for you |
| `/build history feature` | Every version and what happened to it |
| `/build show build` | Open any version's code (with buttons if it's still waiting) |
| `/build rollback feature [version]` | Go back to the previous version that actually ran, or to a version you pick |
| `/build disable` / `/build enable` | Switch a feature off or on. Its data is kept |

### How it stays safe

* **A bad version can't take the bot down.** The feature is loaded inside a guard. If it fails to load, or Discord refuses its slash
  commands, the previous version is loaded again and the bad one is marked *failed*. A first version that fails simply stays
  unloaded. Nothing else in the bot is touched.
* **Startup is guarded too.** At every start the bot rebuilds the feature files from the database and loads them one by one. A feature
  that no longer loads falls back to its previous working version, or is switched off, and staff get a message in the alerts channel.
* **Version history.** Every version is kept in the database (`builder_builds`), with who asked for it and what the AI assumed.
  `/build rollback` makes an older one live again.
* **Database backup first.** A feature can only add its **own new tables**, named `gen_<name>` or `gen_<name>_...`, and only with
  `CREATE TABLE/INDEX IF NOT EXISTS`. Before those are created, a data snapshot of **every** table is written to
  `data/backups/prebuild-<time>-b<build>/` (one `.csv.gz` per table, last 10 kept). If the snapshot fails, nothing is changed. If the
  SQL fails, it is undone. Restoring a table: empty it, then `\copy <table> FROM PROGRAM 'gunzip -c <table>.csv.gz' CSV HEADER`.
  This is on top of the nightly full `pg_dump` in `backups/`.
* **Only you.** Every command and button checks for `OWNER_ID`. The Sect Admin role and the server-owner flag do not count.
  Every build, approval, rollback and switch is written to the audit log.

### What it does NOT protect you from

* **Your review is the real gate.** An approved feature runs inside the bot with the same access as the bot (its token, the whole
  database). The harness limits what the AI is *told* to do, but it does not scan or sandbox the code. Read the attached file, and
  be suspicious of anything that touches `os`, `subprocess`, environment variables, network calls, or tables that aren't `gen_...`.
* **A feature that loads fine but misbehaves** (for example a command that errors, or one that posts the wrong thing) is not
  switched off automatically. Use `/build disable` or `/build rollback`.
* New tables are kept when you roll back or disable (the older code simply ignores them). Drop them by hand if you want them gone.

Generated code lives in the database; `data/generated/` is a copy rewritten at every start, so you don't need to back it up.


**It knows your server.** Every `/build` request is sent with a list of the server's real channels (grouped by category) and roles, with their IDs. So you can write "the rules channel" or "gold role" and it matches the real one. Only names and IDs are sent, never messages or members. The review's "Check these" notes list each match (`Matched: "rules" -> #rules-and-info`), so check those before approving. Very large servers get a truncated list.

## AI providers and the switcher

Every AI feature (ticket helper, the `/ai test`, the dashboard test, `/build`) goes through one provider list. If the provider being
used fails (Gemini's "high demand" 503, a rate limit, a timeout, an expired key), the **next one answers instead**, and the member
never sees the error. A provider that just failed is skipped for a minute (ten minutes for a bad key or model name) so the next
requests don't each wait for it. If it is the **only** provider, a busy answer is retried once.

Set up backups in `.env` (numbered 1 to 6, tried in that order after the main `AI_PROVIDER` / `AI_API_KEY`):

```
AI_FALLBACK_1_NAME=groq
AI_FALLBACK_1_KIND=openai
AI_FALLBACK_1_KEY=<the key>
AI_FALLBACK_1_MODEL=<a model from the provider's list>
AI_FALLBACK_1_BASE_URL=https://api.groq.com/openai/v1
```

`KIND` is `openai` for any OpenAI-compatible service (Groq, OpenRouter, DeepSeek, Mistral, Together), or `gemini` / `anthropic`.
A second Gemini key from a different Google project is also a useful backup, since quotas are per project. Keys live only in `.env`
(never typed into Discord); after editing it, run `docker compose up -d` so the bot restarts with them.

| Command (Sect Admin) | What it does |
|---|---|
| `/ai provider list` | The providers in the order they'll be tried, which is having trouble, and any `.env` block that was skipped and why |
| `/ai provider use <name>` | Pin one to be tried first (it survives restarts). The others still step in if it fails. `auto` goes back to the `.env` order |
| `/ai provider test [name]` | Sends a tiny message to each provider on its own (no fallback) to check its key and model |

Things to know:

* `/ai set model` and `BUILDER_MODEL` name a model for the **main** provider only. A backup always uses the `MODEL` in its own block.
* A backup answers with its own model, so wording and quality differ from the main one. For `/build`, a weaker backup model writes
  weaker code, and a service with a low output limit may refuse the long request and be skipped. You read the code before approving
  either way.
* A skipped block (no key, no model, unknown kind...) is logged at startup and shown at the bottom of `/ai provider list`. It never
  stops the bot.

## Maintenance

### Staff alerts

If the bot can't change someone's level role, usually because its own role is below the level roles or it's missing Manage Roles, it
posts in `orion-alerts`. You can change the channel in `/admin` under Channels. The same problem is reported at most once every 30
minutes.

### Role self-healing

A minute after the bot starts, and again every night at 04:00 in the bot's timezone, it checks every member and corrects any level role
that doesn't match their XP. This also catches people who joined while the bot was offline. Returning members get the rank they earned,
not Earth King.

### Backups

The `backup` container saves a compressed copy of the database to the `backups/` folder when it starts and then every 24 hours, and
deletes copies older than 14 days. Copy that folder somewhere else now and then, such as a cloud drive or a USB stick. A backup on
the same disk is no use if the disk dies. To check it's working, run `docker compose logs backup` and look for `backup ok`.

Remember that `data/giveaway.key` is not part of these backups. Copy it separately.

To restore a backup (swap in the real file name; this replaces the current data):

```
docker compose stop bot
docker compose exec db psql -U orion -d postgres -c "DROP DATABASE orion;" -c "CREATE DATABASE orion;"
docker compose cp backups/orion-YYYY-MM-DD_HHMM.sql.gz db:/tmp/restore.sql.gz
docker compose exec db sh -c "gunzip -c /tmp/restore.sql.gz | psql -U orion -d orion"
docker compose start bot
```

### Updating and tests

```
docker compose up -d --build                           # after changing the code
docker compose run --rm bot python -m pytest -q tests  # run the tests
```

The tests that need a real database (giveaways and polls) are skipped unless `TEST_DATABASE_URL` is set to a Postgres you don't mind
filling with test data.

### Moving to a server

To keep the bot online all the time, move the same folder to a small Linux server (a basic VPS is plenty). Install Docker, copy the
whole project folder including `assets/`, `data/` and your `.env`, run `docker compose up -d --build`, then restore your latest backup
so XP and settings carry over. `restart: unless-stopped` brings everything back after a reboot.

### Experimental: AI ticket handler

Orion can give first-line help in tickets until the Sect has enough staff. It is **off by default** and independent: with it off, or if
the AI service is down, tickets work exactly as normal. Turn it on with `/ai switch on` (or `/admin` -> Experimental feature); it needs
`AI_API_KEY` in `.env` (Gemini, Anthropic or any OpenAI-compatible service).

**How it behaves (hybrid):**
* **Who it is:** Orion always presents itself as **Blue Heaven's personal assistant bot**. This is fixed in code (`orion/ai_brief.py`),
  not a setting: it says plainly that it is an AI bot, never claims to be a person or staff, and ignores requests to play someone else.
* On a new ticket Orion greets the member and answers the form from what it knows (next section). It only states facts that are written
  there; if they don't cover the question it says so, hands over to staff, and records the question in `/ai gaps`.
* It can ask a follow-up question, summarises the conversation for staff, and suggests a ticket category.
* **Handover:** when the question is sensitive, the AI is unsure (below the confidence setting), it hit its reply limit, the member
  asks for a human, or the model's answer can't be read, Orion posts a fixed handover message with a summary and pings the staff
  roles for that ticket type. It then stays quiet. It also steps back the moment a staff member replies or claims the ticket.
* **Never answered by the AI:** *Member Reports*, *Staff Applications* and *Partnerships* (change with `/ai set human-only-types`).
  Orion only greets and notifies staff there.
* **Auto-close:** when the member says they are done (thanks, "that's all", "please close it"), Orion closes the ticket, saves the
  transcript, and the member can reopen it (see below). Both the AI and a code check must agree: the member's last message must be short,
  contain no question, and no "but / also / still / one more" style words. The AI must also be confident, have answered at least once, and
  no staff member may have been involved. A "thanks" after the reply limit closes the ticket instead of handing it over.
  `/ai set auto-close off` disables it.
* **No powers:** the AI has no tools. It cannot ban, kick, punish, change roles or permissions, or delete anything. All it can do is
  send a message, hand over, or close a routine ticket. Member text is treated as data, so "ignore your rules" messages are ignored.

**What Orion knows.** Three layers, all fed into every answer:
1. **Live facts (automatic):** the ticket types and what each is for, the ticket timings, the realms, how XP is earned (the current
   values from `/admin`), and the commands any member can use. Generated from Orion's own settings, so they are never out of date.
2. **Core brief (you write it):** a short overview of Blue Heaven that Orion reads in *every* ticket. `/ai brief set` (up to 4,000 characters).
3. **Knowledge entries (you write them):** rules, events, esports, giveaways, FAQs and so on. The ones that match the question are added to
   that answer.

The fastest way to fill layers 2 and 3 is one file: fill in `knowledge/blue_heaven_knowledge_template.md`, then run **`/ai kb import`** and
attach it. A section that still contains `<<FILL IN>>` is skipped, so you can do it in several sittings. Importing again updates entries with
the same title instead of duplicating them. `/ai kb export` downloads everything Orion knows as the same kind of file.

**Teaching loop:** every time Orion is asked something its knowledge doesn't cover, it hands over to staff **and** records the question.
`/ai gaps` lists them. Add the answers (`/ai kb add`, or edit the file and import it again) and the next member gets an answer.
Orion never guesses: replies are checked against the three layers only. A mistake in the text you write there will be repeated as fact.

**Admin controls** (`/ai`, Sect Admin only): `status`, `switch`, `set` (tone, length, personality, confidence, auto-close, max-replies,
ping-staff, model, human-only-types), `brief set | show`, `kb add | edit | list | toggle | remove | import | export`, `gaps`,
`logs [ticket]`, `test <question>`. Staff can switch it for one ticket with `/ticket ai on|off`. Every AI action is recorded (`/ai logs`),
and settings changes go to the audit log.

### Reopening and retention

Closing a ticket keeps the owner in the channel **read-only**, so **the member who opened it or staff** can press **Reopen** (or use
`/ticket reopen`) until the reopen window ends (24 hours by default); then the channel is deleted. A blacklisted member, or one already at
the open-ticket limit, can't reopen. A reopened ticket is handled by humans (the AI stays off for it).

**Transcripts and ticket records are kept indefinitely.** The transcript stays in the staff log channel and the ticket row (with the
link) stays in the database. There is no automatic deletion. Only the Discord channel is removed after the reopen window.
