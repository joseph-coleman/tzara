#!/usr/bin/env pwsh
# One-command setup for Tzara (Windows / PowerShell).
#   ./setup.ps1
# First run: copies .env.template -> .env, generates a strong random
# POSTGRES_PASSWORD into it, then STOPS so you can review .env before anything
# is built. Run it again to build and start the stack (which also pulls the
# default local models on first start). An existing .env is never overwritten.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker is not installed or not on PATH. Install Docker Desktop first."
    exit 1
}

if (-not (Test-Path .env)) {
    Copy-Item .env.template .env
    # Replace the placeholder BEFORE any container starts: Postgres bakes this
    # value into its data volume on first init and never re-reads it, so it must
    # be the final value the very first time `up` runs.
    $bytes = New-Object 'System.Byte[]' 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    (Get-Content .env) -replace '^POSTGRES_PASSWORD=.*', "POSTGRES_PASSWORD=$pw" | Set-Content .env

    Write-Host "Created .env from .env.template."
    Write-Host "  -> A strong random POSTGRES_PASSWORD was generated for you."
    Write-Host "  -> Review .env now (vaults location, models, GPU overlay, etc.),"
    Write-Host "     then run ./setup.ps1 again to build and start Tzara."
    exit 0
}

Write-Host ".env already exists; using it."

# Read a KEY=value from .env (last assignment wins), trimming whitespace. Fallbacks
# below mirror the defaults in docker-compose.yml so the message matches what
# `ollama-init` actually pulls even when .env omits these.
function Get-EnvVal($key) {
    $line = Select-String -Path .env -Pattern "^$key=" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($line) { return ($line.Line -split '=', 2)[1].Trim() }
    return ""
}

# LLM_* replaced OLLAMA_* when Tzara stopped being Ollama-only; read the new name
# first and fall back to the old one, matching config.py and the compose defaults.
function Get-LlmEnvVal([string]$New, [string]$Old) {
    $v = Get-EnvVal $New
    if (-not $v) { $v = Get-EnvVal $Old }
    return $v
}

$port = Get-EnvVal 'PORT'; if (-not $port) { $port = '8000' }
$chatModel = Get-LlmEnvVal 'LLM_MODEL' 'OLLAMA_MODEL'; if (-not $chatModel) { $chatModel = 'llama3.2:3b' }
$embedModel = Get-LlmEnvVal 'LLM_EMBED_MODEL' 'OLLAMA_EMBED_MODEL'; if (-not $embedModel) { $embedModel = 'embeddinggemma:300m' }
$composeFile = Get-EnvVal 'COMPOSE_FILE'
$llmUrl = Get-LlmEnvVal 'LLM_URL' 'OLLAMA_URL'

# The external-inference overlay puts the local ollamaserver AND the ollama-init
# model-pull job behind an inactive Compose profile, so their images are never
# pulled and nothing is downloaded when it's active.
$externalInference = $composeFile -like '*external-inference*'

# Stop on the easy misconfiguration: LLM_URL aimed at an external server (anything
# other than the in-compose `ollama` host) WITHOUT that overlay would still build/start
# a local Ollama container and download models the user doesn't need. Refuse to build
# so they fix .env first - no wasted images or model pulls.
if (-not $externalInference -and $llmUrl -and ($llmUrl -notmatch '^https?://ollama(:|$)')) {
    # Use Write-Host (not Write-Error): $ErrorActionPreference='Stop' makes Write-Error
    # terminate immediately, which would skip the fix instructions and exit below.
    Write-Host "ERROR: LLM_URL points at an external server ($llmUrl), but the"
    Write-Host "external-inference overlay is not in COMPOSE_FILE. Building now would start a"
    Write-Host "LOCAL Ollama container and download models you don't need."
    Write-Host ""
    Write-Host "To use ONLY your external server, set this in .env, then re-run ./setup.ps1:"
    Write-Host "  COMPOSE_PATH_SEPARATOR=;"
    Write-Host "  COMPOSE_FILE=docker-compose.yml;docker-compose.external-inference.yml"
    exit 1
}

Write-Host "Building and starting Tzara (docker compose up --build -d)..."
docker compose up --build -d

Write-Host ""
Write-Host "Tzara is starting."
if ($externalInference) {
    Write-Host "Using an external inference server ($llmUrl); the local Ollama container"
    Write-Host "and model-pull step are disabled - no models are downloaded."
} else {
    Write-Host "First run pulls the configured models ($chatModel + $embedModel) via the"
    Write-Host "ollama-init service - this can take several minutes on a fresh machine."
}
Write-Host ""
Write-Host "  Readiness check:  curl http://localhost:$port/health"
Write-Host "  Open the wiki:    http://localhost:$port/"
