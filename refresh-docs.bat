@echo off
REM Force-refresh the shipped help documentation into your system vault.
REM
REM Runs inside the SERVER container, which bind-mounts app\ and so sees the live
REM seed tree (the worker bakes its copy at build time).
REM
REM   refresh-docs.bat                             dry run: report what would change
REM   refresh-docs.bat --apply                     refresh docs that already exist
REM   refresh-docs.bat --apply --restore-missing   also re-add absent docs
REM
REM Nothing is ever deleted, and every overwrite commits its pre-image first, so
REM the run is revertable from the vault's git history.

if "%TZARA_CONTAINER%"=="" set TZARA_CONTAINER=tzara-tzaraserver-1

docker inspect -f "{{.State.Running}}" %TZARA_CONTAINER% >nul 2>&1
if errorlevel 1 (
    echo Container '%TZARA_CONTAINER%' is not running. Start the stack first
    echo ^(docker-compose up -d^), or set TZARA_CONTAINER to override.
    exit /b 2
)

docker exec %TZARA_CONTAINER% python -u scripts/refresh_seed_docs.py %*
