@echo off
REM ============================================================================
REM Restart (do NOT rebuild) the app to pick up .env changes.
REM
REM env_file values are read only when a container is created, so a plain
REM `restart` would keep the old environment. --force-recreate makes new
REM containers from the SAME image; --no-build guarantees no rebuild.
REM Only tzaraserver/tzaraworker consume .env, so only those are recreated.
REM
REM Use rebuild.bat instead when worker/jupyter CODE changed - those images
REM bake the source in at build time and a recreate alone won't pick it up.
REM ============================================================================

echo [restart] Recreating server + worker from the existing images...
docker-compose up -d --force-recreate --no-build tzaraserver tzaraworker
if errorlevel 1 (
    echo [restart] FAILED - check docker-compose ps / logs.
    exit /b 1
)

echo [restart] Done. server + worker recreated with the current .env.
