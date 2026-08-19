@echo off
REM ============================================================================
REM Robust rebuild for Tzara's code-bearing containers.
REM
REM WHY this shape: the WORKER and the JUPYTER images BAKE the app code into the
REM image at build time - only the browser server bind-mounts src/. So any change
REM to worker code (agents, the agent-API, the editor-kernel broker, tasks) needs
REM a real IMAGE rebuild, not just a container recreate.
REM
REM The old script tried to force that by `docker rmi <image-name>`, but it (a)
REM used CONTAINER names (tzaraserver-1) where docker-compose wants SERVICE names
REM (tzaraserver), so the kill/rm silently no-op'd, and (b) then the rmi failed
REM because the image was still in use - leaving every container on STALE code.
REM
REM This version rebuilds by compose SERVICE name with `docker-compose build`,
REM which rebuilds the image from the Dockerfile using the current source (no
REM dependence on image names, no rmi needed), then force-recreates the
REM containers from the fresh images. It is cache-aware: unchanged heavy layers
REM (pip installs) stay cached; changed source busts only the COPY layer onward.
REM
REM pgserver / redisserver / ollamaserver are left running (their data + pulled
REM models persist; no reason to churn them on a code rebuild).
REM ============================================================================

echo [rebuild] Building images for the code services (cache-aware)...
docker-compose build tzaraserver tzaraworker jupyterserver
if errorlevel 1 (
    echo [rebuild] BUILD FAILED - aborting. Containers left running OLD code.
    exit /b 1
)

echo [rebuild] Recreating containers from the freshly built images...
docker-compose up -d --force-recreate tzaraserver tzaraworker jupyterserver jupyterserver-agent
if errorlevel 1 (
    echo [rebuild] up FAILED - check `docker-compose ps` / logs.
    exit /b 1
)

echo [rebuild] Done. server + worker + jupyter (both) now running current code.
