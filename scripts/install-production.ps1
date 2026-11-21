# Installs or refreshes the production Orion stack on the Windows hosting PC.
# Safe to re-run: existing .env, orion.env and a WUD-modified compose.yaml are never overwritten.
[CmdletBinding()]
param(
    [string]$ProductionDirectory = 'C:\Orion-Production',
    [string]$RepositoryDirectory = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

function Assert-LastExitCode {
    param([string]$Description)
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Read-DotEnv {
    param([Parameter(Mandatory)][string]$Path)
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $text = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($text) -or $text.StartsWith('#')) { continue }
        $separator = $text.IndexOf('=')
        if ($separator -le 0) { continue }
        $key = $text.Substring(0, $separator).Trim()
        $value = $text.Substring($separator + 1).Trim()
        if ($value.Length -ge 2) {
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $values[$key] = $value
    }
    return $values
}

function Test-Configured {
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    if ($Value -match 'REPLACE_ME|CHANGE_ME|paste-') { return $false }
    return $true
}

function Assert-Required {
    param(
        [Parameter(Mandatory)][hashtable]$Values,
        [Parameter(Mandatory)][string[]]$Keys,
        [Parameter(Mandatory)][string]$File
    )
    $missing = @($Keys | Where-Object { -not (Test-Configured $Values[$_]) })
    if ($missing.Count -gt 0) {
        throw "$File still has unset/placeholder values: $($missing -join ', ')"
    }
}

Write-Host '== Orion production installation ==' -ForegroundColor Cyan

# 1. Verify Docker Desktop and Compose (PowerShell only; no Linux shell commands on the host).
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI not found. Install Docker Desktop with the WSL2 backend and restart PowerShell.'
}
& docker version --format '{{.Server.Os}}' 2>$null
Assert-LastExitCode 'docker version'
$serverOs = (& docker version --format '{{.Server.Os}}').Trim()
if ($serverOs -ne 'linux') {
    throw "Docker is using '$serverOs' containers. Switch Docker Desktop to Linux containers (WSL2) and retry."
}
& docker compose version 2>$null
Assert-LastExitCode 'docker compose version'
Write-Host "Docker server OS: $serverOs"
Write-Host "Compose: $((& docker compose version).Trim())"

# 2. Create the production directory and copy only missing configuration files.
New-Item -ItemType Directory -Force -Path $ProductionDirectory | Out-Null
$composePath = Join-Path $ProductionDirectory 'compose.yaml'
$deployEnvPath = Join-Path $ProductionDirectory '.env'
$appEnvPath = Join-Path $ProductionDirectory 'orion.env'

$copyMap = @(
    @{ Source = (Join-Path $RepositoryDirectory 'deploy\compose.yaml'); Destination = $composePath; Name = 'compose.yaml' },
    @{ Source = (Join-Path $RepositoryDirectory 'deploy\.env.example'); Destination = $deployEnvPath; Name = '.env' },
    @{ Source = (Join-Path $RepositoryDirectory 'deploy\orion.env.example'); Destination = $appEnvPath; Name = 'orion.env' }
)
foreach ($item in $copyMap) {
    if (-not (Test-Path -LiteralPath $item.Source)) {
        throw "Expected repository file is missing: $($item.Source)"
    }
    if (Test-Path -LiteralPath $item.Destination) {
        Write-Host "Keeping existing $($item.Name) (not overwritten)." -ForegroundColor Yellow
    } else {
        Copy-Item -LiteralPath $item.Source -Destination $item.Destination
        Write-Host "Created $($item.Destination). Fill it in before continuing."
    }
}

# 3. Validate deployment and application configuration without printing secret values.
$deployment = Read-DotEnv -Path $deployEnvPath
$application = Read-DotEnv -Path $appEnvPath

Assert-Required -Values $deployment -Keys @(
    'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'GHCR_OWNER',
    'GHCR_USERNAME', 'GHCR_TOKEN', 'GITHUB_REPOSITORY', 'ORION_IMAGE_TAG', 'DEPLOY_WEBHOOK_URL'
) -File '.env'
Assert-Required -Values $application -Keys @('DISCORD_TOKEN') -File 'orion.env'

if ($deployment.ORION_IMAGE_TAG -notmatch '^\d+\.\d+\.\d+$') {
    throw 'ORION_IMAGE_TAG must be a numeric semver such as 1.0.0 (no leading v).'
}
if (-not $deployment.DEPLOY_WEBHOOK_URL.StartsWith('https://discord.com/api/webhooks/')) {
    throw 'DEPLOY_WEBHOOK_URL must be a real HTTPS Discord webhook URL.'
}
if ($deployment.ContainsKey('GHCR_TOKEN') -and $application.ContainsKey('GHCR_TOKEN')) {
    throw 'GHCR_TOKEN must not be present in orion.env.'
}
if ($application.ContainsKey('DB_PASSWORD')) {
    Write-Warning 'orion.env contains DB_PASSWORD; Compose will override it with the deployment .env value.'
}

Write-Host 'Configuration validation passed.' -ForegroundColor Green

# 4. Authenticate against GHCR so the private image can be pulled.
Write-Host 'Authenticating to ghcr.io with the read-only token...'
$deployment.GHCR_TOKEN | & docker login ghcr.io --username $deployment.GHCR_USERNAME --password-stdin
Assert-LastExitCode 'docker login ghcr.io'

# 5-8. Pull and start only this Compose project.
Push-Location $ProductionDirectory
try {
    Write-Host 'Pulling images...'
    & docker compose --env-file .env -f compose.yaml pull bot db backup updater
    Assert-LastExitCode 'docker compose pull'

    Write-Host 'Starting the Orion production stack...'
    & docker compose --env-file .env -f compose.yaml up -d
    Assert-LastExitCode 'docker compose up -d'

    Write-Host ''
    Write-Host '== Container status ==' -ForegroundColor Cyan
    & docker compose --env-file .env -f compose.yaml ps
    Assert-LastExitCode 'docker compose ps'
}
finally {
    Pop-Location
}

Write-Host ''
Write-Host '== First-deployment checklist ==' -ForegroundColor Cyan
Write-Host '1. Watch the bot log:'
Write-Host "   docker compose --env-file '$ProductionDirectory\.env' -f '$composePath' logs -f bot"
Write-Host '2. Confirm the health check becomes healthy and Discord commands respond.'
Write-Host '3. Keep WUD_AUTO_UPDATE=false until you have verified the baseline.'
Write-Host '4. On the host, enable Docker Desktop > Settings > General > Start Docker Desktop when you sign in.'
Write-Host '5. In Windows Power Options, disable automatic sleep while the host is providing Orion.'
Write-Host ''
Write-Host 'Run scripts\deployment-status.ps1 from this repository for a full status summary.' -ForegroundColor Green
