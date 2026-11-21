# Orion Admin Panel

A standalone admin web panel and REST API for Orion. It runs in **its own Docker container**, uses its own Discord OAuth application, and connects to the same PostgreSQL database as the bot. The bot token is never given to this service.

## Features

- **Overview** — data counts, database size, container health.
- **Database** — browse every table, paginate, insert/update/delete rows. `schema_migrations` is protected.
- **Config** — search and edit every JSONB config key.
- **Migrations** — view applied versions. This service never runs migrations.
- **Containers** — health, live logs, and restart buttons for the bot and backup worker.
- **Backups** — list and download daily SQL dumps.
- **REST API** — the same operations as the UI, with bearer-token support for scripts.

## Security model

- Binds to `127.0.0.1:8081` on the host. It is **not** reachable from the internet.
- Discord OAuth2 with `identify`, `guilds`, and `guilds.members.read`. Membership and roles are checked server-side on every login.
- Owner ID gets full access; admin role gets read/write/control; staff role gets read-only.
- Signed, short-lived sessions in HttpOnly/SameSite cookies. CSRF tokens on all writes. Rate limiting enabled.
- Optional bearer token (`ADMIN_API_TOKEN`) for API clients.
- Mounting `/var/run/docker.sock` grants host-level Docker control. Keep the port host-only and restrict who can log in.

## Setup

### 1. Create the Discord application

1. Open https://discord.com/developers/applications and create an application.
2. Under **OAuth2 → General**, add this redirect URI exactly:

   ```text
   http://127.0.0.1:8081/auth/callback
   ```

3. Copy the **Client ID** and generate a **Client Secret**.
4. The panel requests `identify`, `guilds`, and `guilds.members.read`.

### 2. Configure env

```powershell
Copy-Item admin.env.example C:\Orion-Production\admin.env
```

Fill in `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET` (32+ random characters), and optionally `ADMIN_API_TOKEN`. Leave `DATABASE_URL` pointing at `db:5432`.

### 3. Add the service to the production stack

This folder contains its own `compose.yaml`. Copy the whole folder next to the production stack:

```powershell
Copy-Item -Recurse . C:\Orion-Production\admin-panel
```

The compose file joins the existing `orion_orion_internal` network and `orion_pgbackups` volume, and publishes only on `127.0.0.1:8081`. Start it from the copied folder:

```powershell
cd C:\Orion-Production\admin-panel
docker compose -f compose.yaml up -d
```

Open http://127.0.0.1:8081/ and sign in with Discord. The main bot stack is untouched.

## Notes

- Config edits are stored in PostgreSQL immediately, but the running bot caches config in memory. Restart `orion-bot` from the Containers page after changing config keys.
- The panel does not send Discord messages directly. It only manages data and containers.
- To make the panel read-only, set `ADMIN_ALLOW_WRITES=false`.

## Local API check

```powershell
curl http://127.0.0.1:8081/api/health
```
