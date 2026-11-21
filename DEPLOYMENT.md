# Orion production deployment

This document describes the deployment system implemented **inside this repository**. It keeps the existing application, local development workflow and forward-only SQL migrations intact.

The first production deployment is **manual**. Automatic release deployment is implemented but disabled by default. Enable it only after Orion has been verified on the hosting machine.

---

## 1. Detected Orion architecture

Inspection of the repository showed:

| Area | Finding |
|---|---|
| Language/runtime | Python 3.12 |
| Discord library | `discord.py[voice]>=2.7,<3` |
| Package manager | pip (`requirements.txt`, `requirements-dev.txt`) |
| Database | PostgreSQL via `asyncpg`; existing local stack uses `postgres:16-alpine` |
| Configuration | `orion/config.py` reads `os.getenv`; `DISCORD_TOKEN` and a database URL/password are required |
| Migrations | Plain forward-only SQL in `orion/migrations/[0-9][0-9][0-9][0-9]_*.sql`, applied by `orion/migrations/__init__.py` with `schema_migrations` tracking and `pg_advisory_lock(7102026)` |
| Startup | `python -m orion.main`; `setup_hook()` connects the DB, applies migrations, loads cogs and syncs commands |
| Shutdown before this work | `OrionBot.close()` closes the DB, but no explicit SIGTERM/SIGINT handling |
| Logging/error handling | Python `logging`; Discord staff alerts in `orion/alerts.py` |
| Health/readiness before this work | None |
| Disk writes | `DATA_DIR/giveaway.key` and optional `COOKIES_FILE`; custom artwork under `assets/` |

Compatibility decisions made from those facts:

- No Node/TypeScript/Prisma assumptions were added.
- The existing migration runner was **not replaced**. It remains forward-only, transactional per file and guarded by an advisory lock.
- A small read-only `aiohttp` health endpoint was added at `/healthz`; `aiohttp` was already a dependency.
- Explicit SIGTERM/SIGINT handling was added so Docker stops gracefully.
- Persistent data is in Docker named volumes, not the image.
- WUD watches only the Orion image container. PostgreSQL, backup and updater containers are explicitly unwatched.

---

## 2. Files added or changed by this implementation

Created:

- `Dockerfile`
- `.dockerignore`
- `.github/workflows/publish-release.yml`
- `deploy/compose.yaml`
- `deploy/.env.example`
- `deploy/orion.env.example`
- `orion/health.py`
- `scripts/docker-healthcheck.py`
- `scripts/install-production.ps1`
- `scripts/deployment-status.ps1`
- `DEPLOYMENT.md`

Modified:

- `.gitignore` — ignores production env files while retaining the application examples.
- `orion/main.py` — starts the health endpoint and handles SIGTERM/SIGINT gracefully.

The existing root `.env.example` and `docker-compose.yml` were deliberately left untouched. They remain the local development examples/stack.

---

## 3. Architecture overview

### 3.1 Image build and release flow

```text
Your PC: commit + push + publish GitHub Release v1.2.3
                         |
                         v
GitHub Actions (release published only)
  - checkout
  - validate numeric semver
  - build production Docker image
  - push GHCR tags: 1.2.3, v1.2.3, latest
  - add OCI version/revision labels
                         |
                         v
Hosting PC: WUD polls every 6 hours (configurable)
  - sees newer numeric tag for Orion only
  - notifies Discord
  - if WUD_AUTO_UPDATE=true, rewrites the mounted compose file
  - recreates the bot service
  - optional health-gated rollback
```

Ordinary commits and pull requests do **not** build or deploy production.

### 3.2 Hosting stack

`deploy/compose.yaml` defines one Compose project named `orion`:

| Service | Purpose | Notes |
|---|---|---|
| `db` | PostgreSQL 16 | Named volume `pgdata`; health check; no published ports |
| `backup` | Daily logical backups | Named volume `pgbackups`; 14-day retention by default |
| `bot` | Orion | Private GHCR image; `orion.env`; named `orion_data` and `orion_assets` volumes; health check |
| `updater` | WUD | Docker socket only; compose file only; no env files; no dashboard/API |

Networking is an internal bridge network. PostgreSQL is not exposed to the LAN. The Docker socket is mounted only into WUD.

### 3.3 Environment separation

| File | Location | Purpose |
|---|---|---|
| `.env` | `C:\Orion-Production\.env` | Deployment secrets: PostgreSQL credentials, GHCR username/token, update policy |
| `orion.env` | `C:\Orion-Production\orion.env` | Application secrets: Discord token, AI/Twitch keys, role IDs |
| Root `.env.example` | Repository | Existing local-development application example |
| `deploy/.env.example` | Repository | Deployment example, placeholders only |
| `deploy/orion.env.example` | Repository | Production application example, placeholders only |

Compose injects `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_NAME` and `DB_PASSWORD` into the bot from the deployment `.env`. GHCR credentials are never passed to the bot.

### 3.4 Health and graceful shutdown

`orion/health.py` exposes `GET /healthz` on `127.0.0.1:8080` inside the container. It returns HTTP 200 only when:

- the PostgreSQL pool exists and is open; and
- Discord reports ready and the bot is not closed.

Otherwise it returns HTTP 503. Docker's `HEALTHCHECK` uses `scripts/docker-healthcheck.py`. No port is published to the Windows host.

On SIGTERM/SIGINT, `orion/main.py` closes the bot, waits for `bot.start()` to finish, closes the database through `OrionBot.close()`, and cleans up the health server. `stop_grace_period: 60s` gives that time to complete.

---

## 4. One-time GitHub and GHCR setup

### 4.1 Repository settings

1. Open `https://github.com/Greyphr/BLHV-ORION` → **Settings** → **Actions** → **General**.
2. Under **Workflow permissions**, choose **Read and write permissions** so the release workflow can push to GHCR and use the GitHub Actions cache.
3. Confirm the workflow file `.github/workflows/publish-release.yml` is present on `main`.

The workflow itself uses the minimal explicit permissions:

```yaml
permissions:
  contents: read
  packages: write
  actions: write
```

`actions: write` is required by the GHA-backed Docker build cache.

### 4.2 GHCR package visibility

The first successful release run creates the package `ghcr.io/<owner>/orion`.

1. Open GitHub → your profile → **Packages** → `orion`.
2. Set package visibility to **Private** (or public if you intentionally want that).
3. Connect the package to this repository if GitHub asks.
4. Confirm the package has version tags such as `1.0.0`, `v1.0.0` and `latest`.

The `<owner>` must be lower-case. The workflow normalises this automatically.

### 4.3 Read-only token for WUD and Docker login

WUD needs to inspect private tags; the host needs to pull the private image.

1. GitHub → your avatar → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**.
2. Generate a new token with **only** `read:packages`.
3. Set an expiry and store it in your password manager.
4. Put it in `C:\Orion-Production\.env` as `GHCR_TOKEN`.

Do not use this token in `orion.env` or bake it into an image.

### 4.4 Deployment notification webhook

1. In the Discord server, go to **Server Settings** → **Integrations** → **Webhooks** → **New Webhook**.
2. Choose a private staff/admin channel.
3. Copy the generated URL into `DEPLOY_WEBHOOK_URL` in `C:\Orion-Production\.env`.

No webhook URL is hardcoded anywhere in this repository.

WUD notification coverage:

| WUD event | Meaning |
|---|---|
| `available` | New Orion release detected |
| `pre` | Update started |
| `success` | Update completed successfully |
| `failure` | Update failed |
| `rollback` | Rollback happened, including health-gated rollback after an update |

For an Orion health failure that is **not** caused by an update, use `deployment-status.ps1` and the bot logs. WUD's rollback notification covers health failures during an update.

---

## 5. Hosting machine setup

### 5.1 Docker Desktop

On your friend's Windows PC:

1. Install Docker Desktop with the **WSL2** backend.
2. In Docker Desktop → **Settings** → **General**, enable **Start Docker Desktop when you sign in**.
3. In Docker Desktop → **Settings** → **Resources**, allocate enough memory/CPU for Postgres and Python (2–4 GB memory is normally sufficient).
4. Ensure Linux containers are selected.
5. In Windows Power Options, disable automatic sleep while plugged in. Orion cannot run while the host is asleep.
6. Do not publish or forward port 2375/2376. The Docker daemon socket is used only inside the local Compose project.

### 5.2 Install script

From a PowerShell terminal in this repository:

```powershell
cd "C:\Users\User\Downloads\Blue Heaven Files\BLHV BOT\orion-bot"
powershell -ExecutionPolicy Bypass -File .\scripts\install-production.ps1
```

The script:

1. Verifies Docker, Linux containers and Docker Compose.
2. Creates `C:\Orion-Production` if needed.
3. Copies `deploy\compose.yaml`, `deploy\.env.example` → `.env`, and `deploy\orion.env.example` → `orion.env` **only when missing**.
4. Validates required settings without printing secrets.
5. Logs in to GHCR using the read-only token.
6. Pulls the images.
7. Starts the stack.
8. Prints container status and troubleshooting commands.

It is safe to re-run. It never overwrites an existing `.env`, `orion.env` or a WUD-modified `compose.yaml`.

### 5.3 Fill in configuration before running the script

Edit `C:\Orion-Production\.env`:

```dotenv
POSTGRES_DB=orion
POSTGRES_USER=orion
POSTGRES_PASSWORD=<long random password>
GHCR_OWNER=<lowercase GitHub username>
GHCR_USERNAME=<GitHub username>
GHCR_TOKEN=<read:packages PAT>
GITHUB_REPOSITORY=Greyphr/BLHV-ORION
ORION_IMAGE_TAG=<first release version, e.g. 1.0.0>
DEPLOY_WEBHOOK_URL=<Discord webhook URL>
WUD_AUTO_UPDATE=false
WUD_ROLLBACK_ENABLED=false
WUD_CHECK_CRON=0 */6 * * *
BACKUP_RETENTION_DAYS=14
TZ=Africa/Lagos
```

Edit `C:\Orion-Production\orion.env`:

- Set `DISCORD_TOKEN`.
- Set AI/Twitch credentials only if those features are used.
- Leave `GIVEAWAY_ENCRYPTION_KEY` empty unless you already manage that key.

PostgreSQL credentials are supplied through `.env` by Compose; do not duplicate `DB_PASSWORD` in `orion.env`.

---

## 6. Normal development and release workflow

1. Develop and test locally as before.
2. Commit intended changes. Your repository currently contains uncommitted Phase 5 feature work; review and commit that separately and deliberately.
3. Push to GitHub.
4. Publish a GitHub Release whose tag is numeric semver with a leading `v`, for example `v1.2.3`.
5. The workflow publishes:
   - `ghcr.io/<owner>/orion:1.2.3`
   - `ghcr.io/<owner>/orion:v1.2.3`
   - `ghcr.io/<owner>/orion:latest`
6. WUD detects the newer **numeric** tag on its next check and sends a Discord notification.
7. If `WUD_AUTO_UPDATE=true`, WUD rewrites the bot image line in `compose.yaml` and recreates the bot container.

Example release commands:

```powershell
cd "C:\Users\User\Downloads\Blue Heaven Files\BLHV BOT\orion-bot"

git status --short
git add <the files you intend to release>
git commit -m "Orion release 1.0.0"
git push origin main

git tag v1.0.0
git push origin v1.0.0
```

Then in GitHub: **Releases** → **Draft a new release** → select `v1.0.0` → add notes → **Publish release**.

With GitHub CLI installed, the equivalent is:

```powershell
gh release create v1.0.0 --title "Orion v1.0.0" --notes "Describe this release" --verify-tag
```

A release tag such as `v1.0.0-beta` is intentionally rejected by the workflow. WUD is also configured to consider only `^\d+\.\d+\.\d+$` tags, so development/floating tags cannot be deployed.

---

## 7. First manual production deployment

Follow this sequence exactly.

### 7.1 Publish the first release

Use the commands in section 6 with tag `v1.0.0` (or your chosen first version). Confirm the workflow succeeds and the package tags exist.

### 7.2 Prepare the host

Set `ORION_IMAGE_TAG=1.0.0` in `C:\Orion-Production\.env`. Keep:

```dotenv
WUD_AUTO_UPDATE=false
WUD_ROLLBACK_ENABLED=false
```

Run the install script as shown in section 5.2.

### 7.3 Verify PostgreSQL and migrations

```powershell
cd C:\Orion-Production

docker compose --env-file .env -f compose.yaml ps
docker compose --env-file .env -f compose.yaml exec db pg_isready -U orion -d orion
docker compose --env-file .env -f compose.yaml logs --tail=200 bot
docker compose --env-file .env -f compose.yaml exec db psql -U orion -d orion -c "SELECT version, applied_at FROM schema_migrations ORDER BY version;"
```

Look for `Database ready`, `Applied migration ...` and a healthy bot container.

### 7.4 Verify Orion

- `docker compose ... ps` shows `db`, `backup`, `bot` and `updater`.
- Bot health becomes `healthy`.
- The bot logs in and Discord slash commands respond.
- The deployment webhook received a WUD startup/notification if WUD detected the current tag state.
- `scripts\deployment-status.ps1` reports current version, latest GitHub release and status.

### 7.5 Enable automation only after verification

Edit `C:\Orion-Production\.env`:

```dotenv
WUD_AUTO_UPDATE=true
WUD_ROLLBACK_ENABLED=true
```

Restart only WUD so it picks up the change:

```powershell
cd C:\Orion-Production
docker compose --env-file .env -f compose.yaml up -d --no-deps --force-recreate updater
```

`WUD_ROLLBACK_ENABLED=true` enables health-gated rollback. Read section 10.4 first: WUD can revert the Compose project but cannot revert schema changes.

---

## 8. Version tracking

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deployment-status.ps1
```

It reports:

- **Current version** — from the running container's `org.opencontainers.image.version` label.
- **Previous version** — parsed from WUD's `compose.yaml.back` when available.
- **Latest release** — queried from `https://api.github.com/repos/<GITHUB_REPOSITORY>/releases/latest`.
- **Status** — container state, Docker health, update mode and rollback gate.

Versioned images are retained: `WUD_TRIGGER_DOCKERCOMPOSE_LOCAL_PRUNE=false`. Do not manually delete old GHCR versions until you are certain they are no longer needed for rollback.

---

## 9. Maintenance

### 9.1 Logs

```powershell
cd C:\Orion-Production

docker compose --env-file .env -f compose.yaml logs -f bot
docker compose --env-file .env -f compose.yaml logs --tail=200 updater
docker compose --env-file .env -f compose.yaml logs --tail=200 db
```

Logs are JSON-file capped at 10 MB with 5 rotated files per container.

### 9.2 Restart Orion

```powershell
docker compose --env-file .env -f compose.yaml restart bot
```

This does not recreate PostgreSQL and does not delete volumes.

### 9.3 Back up PostgreSQL

A daily backup container already writes to the `pgbackups` named volume. To make an immediate manual backup:

```powershell
cd C:\Orion-Production
$db = ((Get-Content .env | Where-Object { $_ -like 'POSTGRES_DB=*' }) -split '=', 2)[1]
$user = ((Get-Content .env | Where-Object { $_ -like 'POSTGRES_USER=*' }) -split '=', 2)[1]
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmm'
New-Item -ItemType Directory -Force -Path .\manual-backups | Out-Null
docker compose --env-file .env -f compose.yaml exec -T db pg_dump -U $user -d $db -Fc > ".\manual-backups\orion-$stamp.dump"
```

Copy `manual-backups` off the hosting machine regularly. A Docker volume is not an off-site backup.

### 9.4 Restore PostgreSQL

Restoring replaces data. Take a fresh backup first.

```powershell
cd C:\Orion-Production
$db = ((Get-Content .env | Where-Object { $_ -like 'POSTGRES_DB=*' }) -split '=', 2)[1]
$user = ((Get-Content .env | Where-Object { $_ -like 'POSTGRES_USER=*' }) -split '=', 2)[1]

docker compose --env-file .env -f compose.yaml stop bot
Get-Content .\manual-backups\orion-<timestamp>.dump |
  docker compose --env-file .env -f compose.yaml exec -T db pg_restore --clean --if-exists -U $user -d $db
docker compose --env-file .env -f compose.yaml start bot
```

Use restore only for disaster recovery or a planned schema rollback. Never run it as part of a normal image rollback unless the new schema is incompatible.

### 9.5 Rotate credentials

| Credential | Where | After changing |
|---|---|---|
| Discord bot token | `orion.env` | `docker compose ... up -d --force-recreate bot` |
| AI/Twitch keys | `orion.env` | Recreate `bot` |
| GHCR PAT | `.env` | Update token, run `docker login ghcr.io` again, recreate `updater` |
| PostgreSQL password | `.env` and the Postgres role | Update `.env`, change the role in Postgres, recreate `db`/`bot`/`backup`/`updater` carefully |
| Discord webhook | `.env` | Recreate `updater` |

Never change the Postgres password only in `.env` after initial database creation; the stored role password must also be changed.

---

## 10. Rollback and recovery

### 10.1 Roll back a release image

Versioned images remain on GHCR. To roll back from `1.2.3` to `1.2.2`:

```powershell
cd C:\Orion-Production

# 1. Stop future automatic updates first.
#    Edit .env: WUD_AUTO_UPDATE=false
docker compose --env-file .env -f compose.yaml up -d --no-deps --force-recreate updater

# 2. Keep a copy of the current compose file.
Copy-Item .\compose.yaml .\compose.yaml.manual-backup -Force

# 3. Edit compose.yaml manually.
#    Change only the bot service image line back to:
#    image: ghcr.io/<owner>/orion:1.2.2
notepad .\compose.yaml

# 4. Recreate only the bot container.
docker compose --env-file .env -f compose.yaml up -d --no-deps --force-recreate bot

# 5. Verify.
docker compose --env-file .env -f compose.yaml ps
docker compose --env-file .env -f compose.yaml logs --tail=200 bot
```

Do **not** run `docker compose down -v`. Do **not** remove `pgdata`, `pgbackups`, `orion_data` or `orion_assets`.

After a successful rollback, investigate before re-enabling `WUD_AUTO_UPDATE`.

### 10.2 What WUD automatic rollback does

Verified from the current WUD Docker Compose trigger documentation:

- It writes an implicit `.back` copy of the compose file before mutation.
- It uses rename-first archives for replaced containers.
- It waits for the replacement's Docker `HEALTHCHECK` to become terminal.
- On failure it reverts the whole Compose project: recreated services are restored and the compose file backup is copied back.
- `WUD_TRIGGER_DOCKERCOMPOSE_LOCAL_ROLLBACK=false` disables that gate.

Because this project has a real Orion health check, the gate is meaningful. WUD notifications include rollback events.

### 10.3 Database schema compatibility

The image rollback does **not** roll back database schema. Orion migrations are forward-only and recorded in `schema_migrations`.

If the failed release added a migration:

1. Keep the old image running if the old code still works with the new schema.
2. If the old code fails, restore PostgreSQL from a backup taken before that release (section 9.4).
3. Do not manually delete `schema_migrations` rows unless you fully understand the SQL that was applied.

Additive migrations are generally safe to leave in place. Destructive or column-renaming migrations require a planned restore.

### 10.4 Recovering from a failed update

1. Check the Discord webhook for WUD `failure` or `rollback` events.
2. Run `scripts\deployment-status.ps1`.
3. Read `docker compose ... logs --tail=200 updater` and `bot`.
4. If WUD rollback was enabled and succeeded, confirm the running version and health.
5. If rollback was disabled or failed, follow section 10.1.
6. If startup fails because of migrations, follow section 10.3.

---

## 11. Troubleshooting

### GHCR authentication failures

Symptoms: `unauthorized`, `denied`, `login failed`, 401/403 on tag inspection.

Check:

- `GHCR_OWNER` is lower-case and owns the package.
- `GHCR_USERNAME` matches the account owning the PAT.
- `GHCR_TOKEN` has `read:packages` and has not expired.
- The package is private but the PAT can read it.
- Host login works: `docker login ghcr.io -u <username> --password-stdin`.
- WUD was recreated after token rotation.

### Image pull failures

```powershell
docker compose --env-file .env -f compose.yaml pull bot
docker compose --env-file .env -f compose.yaml images
```

Check the exact tag exists in GHCR, the release workflow succeeded, and the hosting machine has internet/DNS access. Version tags remain available even after `latest` moves.

### PostgreSQL connection failures

Check:

```powershell
docker compose --env-file .env -f compose.yaml ps db
docker compose --env-file .env -f compose.yaml logs --tail=100 db
docker compose --env-file .env -f compose.yaml exec db pg_isready -U orion -d orion
```

Orion connects to `db:5432` over the internal network. PostgreSQL has no published host port. Confirm `POSTGRES_USER`, `POSTGRES_DB` and `POSTGRES_PASSWORD` match what was used when `pgdata` was first created.

### Failed health checks

```powershell
docker compose --env-file .env -f compose.yaml ps
docker inspect --format '{{.State.Health.Status}}' orion-bot
docker compose --env-file .env -f compose.yaml logs --tail=200 bot
```

The endpoint requires both database connectivity and Discord readiness. A fresh container can remain unhealthy during the 180-second start period while Discord connects. Persistent 503 means the bot cannot connect to Discord or PostgreSQL.

### Application startup failures

Common causes:

- Missing/placeholder `DISCORD_TOKEN` in `orion.env`.
- Invalid role/channel IDs.
- Database not reachable.
- A cog failed to load; the bot logs `Could not load ...` and posts a staff alert if configured.
- `yt-dlp` auto-update failed; this is non-fatal and logged.

### Migration failures

```powershell
docker compose --env-file .env -f compose.yaml logs --tail=300 bot | Select-String 'migration|Database|ERROR'
docker compose --env-file .env -f compose.yaml exec db psql -U orion -d orion -c "SELECT * FROM schema_migrations ORDER BY version;"
```

Migrations run inside transactions and are protected by `pg_advisory_lock(7102026)`, so two bots cannot apply them concurrently. A failed migration leaves no partial row for that file. Fix the SQL/configuration or restore from backup; do not delete data manually.

### Windows Docker Desktop shutdowns

- Enable Docker Desktop at sign-in.
- Disable sleep/hibernate while plugged in.
- Confirm WSL2 is installed and healthy: `wsl --status`.
- Set all Compose services to `restart: unless-stopped` (already configured).
- After a host reboot, run `scripts\deployment-status.ps1` to confirm everything returned.

### Network interruptions

- WUD rechecks on its configured cron; a missed check is retried later.
- `latest` is only moved by published releases; version tags are immutable recovery points.
- If a pull fails during an update, WUD reports `failure`; containers keep running.
- After connectivity returns, run `docker compose ... pull bot` and verify with the status script.

---

## 12. Remaining manual requirements

The repository cannot safely contain these values. You must supply them on the hosting machine:

- GitHub Actions has permission to write packages.
- GHCR package exists and is private/public as intended.
- Read-only `read:packages` PAT.
- Lower-case GHCR owner.
- Real Discord deployment webhook URL.
- Discord bot token in `orion.env`.
- PostgreSQL password.
- First numeric release version in `ORION_IMAGE_TAG`.
- Windows host power/Docker Desktop auto-start settings.

No real credentials, webhook URLs or tokens are committed in this repository.

---

## 13. WUD documentation references

Configuration was verified against the current official WUD docs:

- Watchers: https://getwud.app/docs/configuration/watchers/
- GHCR registry: https://getwud.app/docs/configuration/registries/ghcr/
- Triggers: https://getwud.app/docs/configuration/triggers/
- Docker Compose trigger: https://getwud.app/docs/configuration/triggers/docker-compose/
- Discord trigger: https://getwud.app/docs/configuration/triggers/discord/
- Server settings: https://getwud.app/docs/configuration/server/

Relevant verified variables used in this implementation:

- `WUD_WATCHER_LOCAL_WATCHBYDEFAULT=false`, `WUD_WATCHER_LOCAL_INCLUDE`/label `wud.watch=true`
- `WUD_REGISTRY_GHCR_LOCAL_USERNAME`, `WUD_REGISTRY_GHCR_LOCAL_TOKEN`
- `WUD_TRIGGER_DOCKERCOMPOSE_LOCAL_FILE`, `_AUTO`, `_BACKUP`, `_PRUNE`, `_ROLLBACK`, `_ROLLBACKWINDOW`, `_ROLLBACKINTERVAL`, `_ROLLBACKGRACE`, `_SELFUPDATE`
- `WUD_TRIGGER_DISCORD_LOCAL_URL`, `_EVENTS`, `_INCLUDEBYDEFAULT`
- `WUD_SERVER_ENABLED=false`

WUD's own docs confirm that `wud.watch=false` labels and opt-in watcher mode can restrict monitoring to only Orion.
