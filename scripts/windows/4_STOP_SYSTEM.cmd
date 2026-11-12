@echo off
cd /d "%~dp0..\.."
docker compose --profile local-llm --profile loadtest down
echo Stopped. Data (index, dashboards) is kept; "docker compose down -v" would delete it.
pause
