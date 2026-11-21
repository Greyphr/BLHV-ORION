# Future Web Platform: TODO and Status

Scope rule: **the website, dashboard and API are NOT built yet.** This file records how Orion is prepared for them and what remains.
Status legend: **Implemented** = done in the bot today. **Prepared** = architecture ready, no feature yet. **Pending** = real work for later.

## Architecture decisions (and what they cost later)

| Decision | Why | Integration impact |
|---|---|---|
| Single bot process; PostgreSQL is the source of truth | Matches the "standalone today" requirement | A future API container reads the same DB; the bot never needs the API running |
| Versioned SQL migrations (`orion/migrations/NNNN_*.sql`, own tiny runner) instead of Alembic | No new dependency; plain SQL can be adopted by Alembic later | When the API arrives, **only one service may run migrations** (the bot, or a one-shot job). Advisory lock already prevents races |
| Services in `orion/services/` (no discord imports) | A FastAPI layer can call them unchanged | Only `cultivation` is extracted so far; the rest still lives in cogs (see Technical debt) |
| Discord IDs are the member key (`user_id BIGINT`) | Discord OAuth2 login maps 1:1 | None |
| Permissions: Patriarch ID + Sect Admin role + Staff role(s), checked in `orion/checks.py` | Simple today | Must move to a DB-backed role-to-permission table before the dashboard (Pending) |
| Tunable settings in the `config` JSONB table | Already editable from `/admin` | API can read/write the same keys; add a whitelist of editable keys |

## Status table

| Area | Implemented | Prepared | Pending |
|---|---|---|---|
| Cultivation (10 realms x 9 ranks, roles, history) | yes | thresholds via `levels.configure()` | admin screen to edit thresholds; store them in `config` and load at start |
| Progression history + audit log tables | yes (`progression_history`, `audit_log`) | `record_audit()` available to any code | audit calls on config changes, tickets, announcements, moderation |
| Services layer | `services/cultivation.py` | pattern established | tickets, announcements, events, giveaways, polls services |
| Migrations | yes | | move to Alembic only if the API team wants it |
| Docker | bot + db + backup compose, Dockerfile | | api, web, redis, reverse proxy (do not add empty containers) |
| Secrets | env vars only, no secrets in repo | | secrets manager for production |
| Member profiles | `/profile` (cultivation, join date, activity, achievements, events; staff section) via `services/profiles.get_profile()` | the same function can back `GET /members/{id}` | richer activity stats (e.g. active days, streaks) |
| Tickets | six Blue Heaven categories, claim/transcripts/etc. (from Menace); members can reopen; transcripts kept indefinitely (decision: no retention period) | | member-visible ticket list, participants model if needed |
| AI ticket handler | hybrid handler: knowledge base, per-ticket and global switch, tone/length/confidence settings, escalation + summaries, auto-close of routine tickets, activity log (`/ai`) | decision rules in `ticket_ai.py` are pure and reusable by an API | dashboard screens for the same settings; embeddings-based knowledge search if keyword matching proves too weak |
| Achievements | tables, `/achievement` commands, shown on profiles | | automatic unlock conditions, notifications, XP/role rewards, admin UI (Phase 7) |
| Events | event + sign-up store, waitlist promotion, history on profiles (the Menace upload shipped the cog without this store) | | attendance commands, cancel, reminders, recurring events (Phase 7) |
| Announcements | builder, 8 built-in templates, author line, link buttons, scheduling (`scheduled_announcements`), edit, delete, audit entries | `announce` data (`Draft` JSON) is plain and API-ready | dashboard embed builder; destination-channel allow-list per role |
| Moderation | cases, warn/note/timeout/kick/ban/unban/pardon, temp bans, reports, spam + raid detection, escalation, audit entries; rules in `services/moderation.py` | `ModStore` + the pure rules are API-ready | dashboard case browser; appeals workflow; an `on_member_ban` listener to record bans made outside Orion; link reports to cases |
| Dashboard / API / OAuth2 | | | everything below |

## A. Dashboard
- Pages: Overview, Members, Cultivation, Tickets, Announcements, Moderation, Achievements, Events, AI settings, Configuration, Audit log.
- Login: Discord OAuth2 (`identify`, `guilds.members.read`), verify guild membership and roles server-side.
- Staff management screen: map Discord roles to permissions.
- Patriarch (`OWNER_ID`) always has full access.

## B. API (FastAPI, separate container)
- Auth: OAuth2 code flow, short-lived signed session, CSRF protection, rate limiting.
- Endpoints to expose first (each wraps a service): `GET /members/{id}`, `GET /leaderboard`, `POST /members/{id}/xp` (calls `adjust_member_xp`),
  `GET /progression/{id}`, `GET /audit`, `GET/PUT /config/{key}` (whitelist), tickets list/transcript, announcements CRUD, moderation cases.
- Anything that needs Discord (send a message, change a role) must be requested **from the bot**, not done by the API with the bot token.
  Choose one: a Redis queue the bot consumes, or an internal HTTP endpoint on the bot reachable only inside the Docker network.
- The frontend never receives the bot token, DB credentials or any secret.

## C. Database
- Reusable now: `users`, `xp_period`, `config`, `progression_history`, `audit_log`, tickets, announcements, polls, giveaways.
- Added in migration 0006: `mod_cases` (id = case number), `mod_reports`.
- Added in migration 0005: `scheduled_announcements`.
- Added in migration 0004: `tickets.ai_state / ai_replies / ai_category / ai_summary`, `ai_knowledge`, `ai_logs`.
- Added in migration 0003: `events`, `event_signups` (with `attended`), `achievements`, `member_achievements`, `users.joined_server_at`.
- Still to add: `staff_permissions` (role_id, permission), `ai_conversations`, `knowledge_base`,
  dashboard sessions (if not stateless).
- `member_profiles` is not needed yet: `users` plus `progression_history` cover it; add columns when join date and activity stats are built.
- Keep one writer per rule: XP changes go through `Database.adjust_xp` / services only.

## D. Infrastructure
- Containers to add later: `api`, `web`, `redis` (only when a queue or cache is actually used), `proxy` (Caddy or Traefik, automatic HTTPS).
- Compose: extend the existing file; the bot service must keep working alone.
- Domain, DNS, HTTPS, hosting choice: undecided.

## E. Security
- Enforce permissions in services/API, never only in the UI. The dashboard must not bypass `checks.py` rules.
- Sessions: HttpOnly secure cookies, rotation, logout. API: input validation (pydantic), rate limits, audit every write.
- Backups: the compose `backup` container dumps nightly and keeps 14 days; test a restore, and also back up `data/giveaway.key`.
- Secrets: env or a secrets manager; rotate the bot token if it ever leaks.

## F. Integration roadmap and order
1. Finish bot phases: member profiles, tickets + AI handler, announcements polish, moderation, achievements, events.
2. Extract the remaining logic from cogs into services (tickets and announcements first).
3. DB-backed permissions table + audit calls everywhere.
4. Build the API (read-only first), then writes, then the dashboard.
5. Add the reverse proxy and deploy.

### Technical debt / limitations to know about
- `cogs/ticket.py` (~65 KB), `cogs/dashboard.py` and `cogs/announce.py` still mix rules and Discord code.
- `Database` is one class composed of several stores; shared method names are guarded by `tests/test_orion_db.py`.
- Config values are cached in memory by the bot: a change made directly in the DB (or by an API) is not seen until restart, unless a
  reload mechanism (Redis pub/sub or a polled version key) is added.
- Placeholder banner art is generated, not designed.

## Product decisions recorded (owner answers)
- Orion starts on a **fresh database**; no Menace data is migrated.
- **Events and achievements do not grant XP.** XP comes only from messages and voice.
- AI may auto-close routine tickets; members may reopen tickets; **no transcript retention period** (kept indefinitely).
- Kept from Menace: Twitch live alerts, polls, welcome card, DM banner, music, giveaways, beta-keys (dormant until its role IDs are set).
- Moderation defaults chosen by the developer (owner did not specify): staff can warn/timeout/note, kick/ban/unban/pardon need the Sect Admin
  role, escalation off, spam and raid detection alert-only. All are changeable with `/mod set`.
