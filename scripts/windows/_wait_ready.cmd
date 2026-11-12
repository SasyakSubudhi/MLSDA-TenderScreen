@echo off
rem Waits until the API answers and at least one research and one verifier worker are registered.
set /a TRIES=0
:loop
set /a TRIES+=1
if %TRIES% gtr 180 (
  echo The system did not become ready in 15 minutes. Run: docker compose ps   and   docker compose logs indexer
  exit /b 1
)
timeout /t 5 /nobreak >nul
curl -s http://localhost:8000/v1/system > "%TEMP%\ts_system.json" 2>nul
findstr /c:"research:" "%TEMP%\ts_system.json" >nul || goto loop
findstr /c:"verifier:" "%TEMP%\ts_system.json" >nul || goto loop
exit /b 0
