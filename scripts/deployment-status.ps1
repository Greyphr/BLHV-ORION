# Read-only deployment status for the Windows hosting PC.
# Never prints tokens, webhook URLs, Discord tokens or database passwords.
[CmdletBinding()]
param(
    [string]$ProductionDirectory = 'C:\Orion-Production'
)

$ErrorActionPreference = 'Stop'

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

function Get-ComposeBotImage {
    param([AllowNull()][string[]]$Lines)
    if ($null -eq $Lines) { return $null }
    $inBot = $false
    foreach ($line in $Lines) {
        if ($line -match '^\s{2}([A-Za-z0-9_-]+):\s*$') {
            $inBot = ($matches[1] -eq 'bot')
            continue
        }
        if ($inBot -and $line -match '^\s{4}image:\s*(.+?)\s*$') {
            return $matches[1]
        }
    }
    return $null
}

function Get-DockerFormat {
    param([string]$ContainerId, [string]$Format)
    $value = & docker inspect --format $Format $ContainerId 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($value | Select-Object -First 1).Trim()
}

$composePath = Join-Path $ProductionDirectory 'compose.yaml'
$deployEnvPath = Join-Path $ProductionDirectory '.env'
if (-not (Test-Path -LiteralPath $composePath)) { throw "Missing $composePath" }
if (-not (Test-Path -LiteralPath $deployEnvPath)) { throw "Missing $deployEnvPath" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker CLI not found.' }

$deployment = Read-DotEnv -Path $deployEnvPath
$composeLines = Get-Content -LiteralPath $composePath

Write-Host '== Orion deployment status ==' -ForegroundColor Cyan
Write-Host "Production directory: $ProductionDirectory"
Write-Host "Update mode:          $(if ($deployment.WUD_AUTO_UPDATE -eq 'true') { 'AUTOMATIC' } else { 'MANUAL (first deployment / verification)' })"
Write-Host "Rollback gate:        $(if ($deployment.WUD_ROLLBACK_ENABLED -eq 'true') { 'enabled' } else { 'disabled' })"
Write-Host "Check schedule:       $($deployment.WUD_CHECK_CRON)"

$desiredImage = Get-ComposeBotImage -Lines $composeLines
$previousImage = $null
$botId = $null
$updaterId = $null

Push-Location $ProductionDirectory
try {
    $botId = (& docker compose --env-file .env -f compose.yaml ps -q bot 2>$null | Select-Object -First 1)
    $updaterId = (& docker compose --env-file .env -f compose.yaml ps -q updater 2>$null | Select-Object -First 1)

    Write-Host ''
    Write-Host '== Container status ==' -ForegroundColor Cyan
    & docker compose --env-file .env -f compose.yaml ps
    if ($LASTEXITCODE -ne 0) { Write-Warning 'docker compose ps did not return a clean result.' }

    if ($botId) {
        $state = Get-DockerFormat -ContainerId $botId -Format '{{.State.Status}}'
        $health = Get-DockerFormat -ContainerId $botId -Format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
        $image = Get-DockerFormat -ContainerId $botId -Format '{{.Config.Image}}'
        $imageId = Get-DockerFormat -ContainerId $botId -Format '{{.Image}}'
        $version = Get-DockerFormat -ContainerId $botId -Format '{{index .Config.Labels "org.opencontainers.image.version"}}'
        $revision = Get-DockerFormat -ContainerId $botId -Format '{{index .Config.Labels "org.opencontainers.image.revision"}}'

        Write-Host ''
        Write-Host '== Current Orion version ==' -ForegroundColor Cyan
        Write-Host "Container state:  $state"
        Write-Host "Health:           $health"
        Write-Host "Running image:    $image"
        Write-Host "Image ID:         $($imageId.Substring(0, [Math]::Min(12, $imageId.Length)))"
        Write-Host "Version label:    $(if ($version) { $version } else { '(not labelled)' })"
        Write-Host "Source revision:  $(if ($revision) { $revision.Substring(0, [Math]::Min(12, $revision.Length)) } else { '(not labelled)' })"
    } else {
        Write-Warning 'Orion bot container is not running.'
    }

    Write-Host ''
    Write-Host '== Desired / previous version ==' -ForegroundColor Cyan
    Write-Host "Compose image:    $(if ($desiredImage) { $desiredImage } else { '(could not parse bot image line)' })"

    if ($updaterId) {
        $backup = & docker exec $updaterId sh -c 'cat /production/compose.yaml.back 2>/dev/null || true' 2>$null
        if ($LASTEXITCODE -eq 0 -and $backup) {
            $previousImage = Get-ComposeBotImage -Lines ($backup -split "`n")
        }
    }
    Write-Host "Previous image:   $(if ($previousImage) { $previousImage } else { '(unknown; WUD has no rollback backup yet)' })"
}
finally {
    Pop-Location
}

# Latest release comes from GitHub, not from the Docker :latest tag.
$latest = '(unavailable)'
$repo = $deployment.GITHUB_REPOSITORY
if ($repo) {
    $headers = @{ 'User-Agent' = 'orion-deployment-status'; Accept = 'application/vnd.github+json' }
    if ($deployment.ContainsKey('GITHUB_STATUS_TOKEN') -and $deployment.GITHUB_STATUS_TOKEN) {
        $headers['Authorization'] = "Bearer $($deployment.GITHUB_STATUS_TOKEN)"
    }
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $headers -TimeoutSec 15
        $latest = $release.tag_name
    } catch {
        $latest = "(could not query GitHub: $($_.Exception.Message))"
    }
}
Write-Host ''
Write-Host '== Latest available release ==' -ForegroundColor Cyan
Write-Host "GitHub latest:    $latest"

Write-Host ''
Write-Host '== Useful commands ==' -ForegroundColor Cyan
Write-Host "Logs:     docker compose --env-file '$ProductionDirectory\.env' -f '$composePath' logs -f bot"
Write-Host "Updater:  docker compose --env-file '$ProductionDirectory\.env' -f '$composePath' logs --tail=100 updater"
Write-Host "Restart:  docker compose --env-file '$ProductionDirectory\.env' -f '$composePath' restart bot"
