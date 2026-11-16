@echo off
title TenderScreen - load tests (about 35 minutes)
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."
docker info >nul 2>&1 || (echo Docker Desktop is not running. & pause & exit /b 1)

rem ---------------------------------------------------------------------------
rem This script NEVER reads, writes, copies or deletes the project .env.
rem Every setting it needs is a non-secret value in a throw-away env file under %TEMP%
rem that is passed with "docker compose --env-file". The model is forced back to the
rem built-in stub, so no hosted provider (and no key) can be used or billed by a load test.
rem Optional knobs (defaults reproduce the documented matrix):
rem   LOADTEST_WORKERS="1 3"  LOADTEST_USERS="1 5 10 20 40"  LOADTEST_DURATION=90s
rem ---------------------------------------------------------------------------
if not defined LOADTEST_WORKERS set "LOADTEST_WORKERS=1 3"
if not defined LOADTEST_USERS set "LOADTEST_USERS=1 5 10 20 40"
if not defined LOADTEST_DURATION set "LOADTEST_DURATION=90s"
set "LT_ENV=%TEMP%\tenderscreen-loadtest.env"
set "LT_RC=0"

rem a stub selection exported in this shell would beat the env file: clear it
for %%V in (LLM_PROVIDER_NAME LLM_BASE_URL LLM_MODEL LLM_API_KEY LLM_API_KEY_FILE STUB_MODE STUB_DELAY_MS STUB_JITTER_MS AGENT_WORKERS LLM_CACHE_ENABLED RETRIEVAL_CACHE_ENABLED RUN_CACHE_ENABLED RATE_LIMIT_PER_MIN DEMO_MODE) do set "%%V="

echo ============================================================
echo  Load tests: research workers %LOADTEST_WORKERS%  x  users %LOADTEST_USERS%
echo  then the LLM-cache experiment. Takes about 35 minutes.
echo  Do not use the laptop for heavy work meanwhile.
echo ============================================================
if not exist loadtest\results mkdir loadtest\results
if exist loadtest\results\matrix.json del loadtest\results\matrix.json
if exist loadtest\results\cache_experiment.json del loadtest\results\cache_experiment.json

for %%W in (%LOADTEST_WORKERS%) do (
  echo.
  echo ---- !date! !time!  research workers = %%W
  call :write_env false %%W || goto :fail
  docker compose --env-file "%LT_ENV%" up -d --scale agent-worker=%%W || goto :fail
  call "%~dp0_wait_ready.cmd" || goto :fail
  timeout /t 20 /nobreak >nul
  for %%U in (%LOADTEST_USERS%) do (
    set /a R=%%U/5
    if !R! lss 1 set R=1
    echo ---- running w%%W_u%%U  ^(%LOADTEST_DURATION% + drain^)
    docker compose --env-file "%LT_ENV%" --profile loadtest run --rm locust --headless -u %%U -r !R! -t %LOADTEST_DURATION% --stop-timeout 120 --host http://api:8000 --csv /mnt/locust/results/w%%W_u%%U --only-summary >nul 2>&1 || goto :fail
    timeout /t 10 /nobreak >nul
    docker compose --env-file "%LT_ENV%" --profile loadtest run --rm --entrypoint python locust /mnt/locust/summarize.py matrix w%%W_u%%U %%W %%U || goto :fail
  )
)
echo.
echo ---- LLM response cache experiment (3 workers, 10 users)
for %%C in (false true) do (
  call :write_env %%C 3 || goto :fail
  docker compose --env-file "%LT_ENV%" up -d --scale agent-worker=3 || goto :fail
  docker compose --env-file "%LT_ENV%" exec -T redis sh -c "redis-cli --scan --pattern 'llm:cache:*' | xargs -r redis-cli del" >nul
  call "%~dp0_wait_ready.cmd" || goto :fail
  timeout /t 20 /nobreak >nul
  echo ---- running cache_%%C
  docker compose --env-file "%LT_ENV%" --profile loadtest run --rm locust --headless -u 10 -r 2 -t %LOADTEST_DURATION% --stop-timeout 120 --host http://api:8000 --csv /mnt/locust/results/cache_%%C --only-summary >nul 2>&1 || goto :fail
  timeout /t 10 /nobreak >nul
  docker compose --env-file "%LT_ENV%" --profile loadtest run --rm --entrypoint python locust /mnt/locust/summarize.py cache cache_%%C %%C || goto :fail
)
echo.
docker compose --env-file "%LT_ENV%" --profile loadtest run --rm --entrypoint python locust /mnt/locust/summarize.py table || goto :fail
goto :cleanup

:fail
set "LT_RC=1"
echo.
echo *** LOAD TEST FAILED - results above are incomplete. Cleaning up. ***

:cleanup
rem back to the normal configuration (the project's own defaults / .env interpolation) and remove our temp file
if exist "%LT_ENV%" del "%LT_ENV%"
docker compose up -d >nul 2>&1
echo.
if "%LT_RC%"=="0" (
  echo DONE. Results are in loadtest\results  ^(matrix.md, matrix.json, cache_experiment.md, Locust CSVs^)
) else (
  echo Load test did not complete. Fix the error above and run this file again.
)
if not defined LOADTEST_NO_PAUSE pause
exit /b %LT_RC%

:write_env
rem %1 = LLM_CACHE_ENABLED, %2 = research workers. Non-secret values only.
(
echo RATE_LIMIT_PER_MIN=0
echo LLM_CACHE_ENABLED=%1
echo LLM_PROVIDER_NAME=stub
echo LLM_BASE_URL=http://llm-stub:8200/v1
echo STUB_MODE=ok
echo AGENT_WORKERS=%2
) > "%LT_ENV%"
exit /b %errorlevel%
