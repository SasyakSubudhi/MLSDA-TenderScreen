@echo off
title TenderScreen - start
cd /d "%~dp0..\.."
docker info >nul 2>&1 || (echo Docker Desktop is not running. Start Docker Desktop, wait until it says "Engine running", then run this again. & pause & exit /b 1)
echo ============================================================
echo  Starting TenderScreen (first time: 3-6 minutes, needs internet)
echo ============================================================
docker compose up --build -d
if errorlevel 1 (echo docker compose failed - see the messages above. & pause & exit /b 1)
echo.
echo Waiting until the agents are ready ...
call "%~dp0_wait_ready.cmd" || (pause & exit /b 1)
echo.
docker compose ps
echo.
echo READY.  Web client: http://localhost:8000    Grafana: http://localhost:3000/d/tenderscreen
echo         Jaeger:     http://localhost:16686   Prometheus: http://localhost:9090/targets
start "" http://localhost:8000
start "" http://localhost:3000/d/tenderscreen
pause
