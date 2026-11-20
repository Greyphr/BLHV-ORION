# Orion: Menace Codebase Review

Review of `MNC-Menace-Bot` against the Blue Heaven spec (section 14).
Bot name: **Orion** (the spec's working name "Tianling" is replaced throughout).

## 1. What Menace is today

| Area | Finding |
|---|---|
| Size | ~12,800 lines of Python, 240 tests (188 pass locally, 57 skipped because they need a real PostgreSQL via `TEST_DATABASE_URL`) |
| Framework | discord.py 2.7 (cogs, app commands, persistent views) |
| Database | PostgreSQL 16 via raw `asyncpg`, no ORM |
| Schema management | `CREATE TABLE IF NOT EXISTS` strings run at every startup. **No migrations.** |
| Config | `.env` + a `config` JSONB key/value table cached in memory (`db.get` / `db.set`) |
| Deployment | Dockerfile (python 3.12-slim) + compose with `db`, `bot`, and a 24h `pg_dump` backup container. No Redis. |
| Permissions | Hard-coded Menace role IDs (Anbu, MNC, staff list) in `config.py` and `checks.py` |
| AI | `ai.py`: small provider-agnostic client (Gemini / Anthropic / OpenAI-compatible), used only for one Rivals Fantasy ticket type |
| Not found | No breakthrough mechanic and no LFG anywhere in the code, so there is nothing to remove for those two items. |

## 2. Reusable (carry over mostly as-is)

- **Startup resilience** in `main.py`: each cog loads in try/except, so one broken feature does not take the bot down; failures are reported to a staff channel (`alerts.py`).
- **Database URL builder** in `config.py` (percent-encodes the password) and `_int_env` / `_id_list_env` helpers.
- **Atomic XP writes** in `db.py` (`FOR UPDATE` inside a transaction, daily bonus, voice cap, weekly/monthly periods). Sound design; rework the numbers, keep the pattern.
- **Anti-abuse in `cogs/leveling.py`**: message cooldown, duplicate-text check, minimum real characters after stripping links/emoji, voice requires 2+ humans, mute/deaf skipped, AFK channel skipped, excluded channels and roles. Covers most of spec 3.4.
- **Role self-healing** in `roles.py`: hierarchy diagnostics, startup and nightly resync, one-role-replaces-another logic. Directly serves spec 3.5.
- **Ticket system** (`tickets.py`, `ticket_db.py`, `ticket_ui.py`, `cogs/ticket.py`): types with intake questions, claim / unclaim / transfer / add / remove / reopen, priority, notes, blacklist, HTML + text transcripts, ratings, auto-close, per-member advisory-lock open limit. This already covers nearly all of spec 5.3.
- **Announcement builder** (`cogs/announce.py`): draft, preview, templates, edit published messages, images stored in DB, SSRF check on image URLs (`_is_public`). Covers most of spec 6.
- **Events** (`events.py`, `cogs/events.py`): join / leave buttons, slots and waitlist, DM promotion, flexible time parsing, tournament format option.
- **Pure, testable modules** (`levels.py`, `events.py`, `tickets.py` helpers, `timeutil.py`).
- **Docker backup container** and log rotation settings.
- **`ai.py` provider layer** (see Refactor for what must change).

## 3. Refactor

1. **Migrations.** Replace startup `CREATE TABLE IF NOT EXISTS` with Alembic (or equivalent). The ticket schema already contains an inline `ALTER` + `UPDATE` data migration, which is the first sign of this breaking down. Required by both docs.
2. **Business logic out of cogs.** `cogs/ticket.py` is 65 KB, `cogs/dashboard.py` 40 KB, `cogs/announce.py` 29 KB, with rules mixed into Discord handlers. Introduce a `services/` layer (cultivation, members, tickets, moderation, announcements, achievements, events) that takes plain IDs and returns plain results, with cogs as thin adapters. This is the single biggest piece of work for the future FastAPI layer.
3. **`Database` god-object.** `Database(GiveawayStore, PollStore, TicketStore, KeyStore)` plus a config cache. Split into per-domain repositories sharing one pool.
4. **Hard-coded Menace identity.** Role IDs, channel IDs, the voice-perk user ID, the "Rivals Fantasy" prompt, branding strings, `nexus` leftovers (DB name/user, `custom_id`s such as `nexus:event:join`) need to become Blue Heaven config. Changing `custom_id` prefixes means old persistent buttons stop working, so do it once, at cutover.
5. **Single-guild assumption.** `config.GUILD_ID` is referenced in 13 places. Fine for one server; just keep it in one config object rather than scattered.
6. **Permissions.** Replace Anbu / XP role / staff list with a role-to-permission model (module + action), with the Sect Patriarch as full access. Store it in the database so a dashboard can edit it later and enforce it in services, not in command decorators.
7. **Member identity.** `users` is keyed by Discord ID with XP columns only. Expand to a member profile (joined date, activity stats) without making core tables Discord-specific beyond the ID.
8. **AI module.** Currently one prompt, one ticket type, a single answer paragraph. Needs: knowledge base, confidence + escalation, summaries, per-ticket toggle, activity log, and model/length/tone settings (spec 5.4). Keep the "model writes text, code makes decisions" principle; it matches the spec's no-ban/no-kick rule.
9. **Test coverage for DB code** only runs with a live PostgreSQL. Add a compose-based test profile or CI job so the 57 skipped tests actually run.

## 4. New development

- **Realm / sub-rank progression:** 10 realms x 9 sub-ranks (90 internal ranks), configurable thresholds, one Discord role per realm, progression history table, notification modes (every sub-rank / realm only / milestones).
- **Cultivation Discord category:** `#realm-announcements`, `#cultivation-leaderboard`, `#cultivation-logs` (staff-only).
- **Leaderboard** showing realm, sub-rank, XP and progress to next rank. (Existing board has all-time/weekly/monthly; spec wants one global board.)
- **Member profiles** with join date, activity stats, achievements, event history.
- **Moderation module** (warnings, timeouts, kicks, bans, case numbers, reports, spam and raid protection). Nothing equivalent exists.
- **Achievements module** (definitions, unlock conditions, rarity, notifications, manual grant).
- **Audit log** of administrative actions and configuration changes.
- **Ticket additions:** the six Blue Heaven categories, participants model if needed, transcript retention policy.
- **Event additions:** attendance tracking, reminders, cancellation, history, recurring events (open decision).
- **Redis** only if background jobs/caching genuinely need it. Current loops use `discord.ext.tasks` and the DB, and the future-platform doc says not to add services without a use.
- **`FUTURE_WEB_PLATFORM_TODO.md`** with Implemented / Prepared / Pending status.

## 5. Remove (not carried into Orion)

These are Menace-specific and not in the Blue Heaven spec: music player (yt-dlp, Spotify, Deno, ffmpeg in the image), giveaways (and the encrypted reward store), Rivals Fantasy beta keys, Twitch live alerts, polls, welcome-card renderer and DM banner (unless you want a Blue Heaven welcome card), and the Menace level-up and milestone artwork. Removing the music stack also drops ffmpeg/libopus/Deno from the Docker image and the voice-encryption pin on `davey`.

Please confirm this list. Polls, giveaways and the welcome card are the ones people most often want to keep.

## 6. Open items that block specific phases

| Needed for | Question |
|---|---|
| Phase 2 | XP rates, cooldown, voice rules, and the threshold curve for 90 sub-ranks (Menace's curve tops out at level 100) |
| Phase 2 | Do events or achievements grant XP? |
| Phase 1 | Blue Heaven guild ID, realm role names/colours, staff role names and who counts as Sect Patriarch |
| Phase 4 | Can the AI auto-close routine tickets? Can members reopen? Transcript retention period? |
| Phase 1 | Is existing Menace data being migrated, or is Orion a fresh database? |

## 7. Suggested Phase 1 scope

1. New package `orion/` with `config`, `db` (single pool + repositories), Alembic baseline, `services/`, `cogs/` skeleton.
2. Permission model and audit log table.
3. Dockerfile slimmed (no music dependencies); compose with `db`, `bot`, `backup` only.
4. Port the resilient cog loader, alerts, and ticket / announcement / event modules behind services in later phases.
5. `FUTURE_WEB_PLATFORM_TODO.md` started with Implemented / Prepared / Pending sections.

## Addendum (found during Phase 3)

The uploaded Menace source contained the events cog and its tests for the embed helpers, but **not** the events database layer
(`create_event`, `join_event`, `leave_event` ... were called but defined nowhere) and the cog was not in the load list, so events
could never have run. Orion now has that store (`orion/event_db.py`, migration 0003) with transactional sign-ups and waitlist promotion.

## Addendum 2 (Phase 4)

The Menace AI helper was a one-shot, single-ticket-type flow tied to its beta role. It was replaced by a general hybrid handler
(`orion/ticket_ai.py` = decision rules, `orion/ticket_ai_runner.py` = Discord side, `orion/cogs/ai_admin.py` = `/ai` controls). Menace's
Rivals Fantasy ticket type and its auto role-grant were removed from the ticket system; the beta-keys module remains but is dormant.
