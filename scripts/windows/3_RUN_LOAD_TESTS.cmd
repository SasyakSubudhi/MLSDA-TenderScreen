@echo off
title TenderScreen - load tests (about 35 minutes)
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."
docker info >nul 2>&1 || (echo Docker Desktop is not running. & pause & exit /b 1)
echo ============================================================
echo  Load tests: research workers 1 and 3 x users 1,5,10,20,40
echo  then the LLM-cache experiment. Takes about 35 minutes.
echo  Do not use the laptop for heavy work meanwhile.
echo ============================================================
if exist .env copy /y .env .env.before-loadtest >nul
(
echo RATE_LIMIT_PER_MIN=0
echo LLM_CACHE_ENABLED=false
) > .env
if not exist loadtest\results mkdir loadtest\results
if exist loadtest\results\matrix.json del loadtest\results\matrix.json
if exist loadtest\results\cache_experiment.json del loadtest\results\cache_experiment.json
for %%W in (1 3) do (
  echo.
  echo ---- !date! !time!  research workers = %%W
  docker compose up -d --scale agent-worker=%%W
  call "%~dp0_wait_ready.cmd"
  timeout /t 20 /nobreak >nul
  for %%U in (1 5 10 20 40) do (
    set /a R=%%U/5
    if !R! lss 1 set R=1
    echo ---- running w%%W_u%%U  ^(90 s + drain^)
    docker compose --profile loadtest run --rm locust --headless -u %%U -r !R! -t 90s --stop-timeout 120 --host http://api:8000 --csv /mnt/locust/results/w%%W_u%%U --only-summary >nul 2>&1
    timeout /t 10 /nobreak >nul
    docker compose --profile loadtest run --rm --entrypoint python locust /mnt/locust/summarize.py matrix w%%W_u%%U %%W %%U
  )
)
echo.
echo ---- LLM response cache experiment (3 workers, 10 users)
for %%C in (false true) do (
  (
  echo RATE_LIMIT_PER_MIN=0
  echo LLM_CACHE_ENABLED=%%C
  ) > .env
  docker compose up -d --scale agent-worker=3
  docker compose exec -T redis sh -c "redis-cli --scan --pattern 'llm:cache:*' | xargs -r redis-cli del" >nul
  call "%~dp0_wait_ready.cmd"
  timeout /t 20 /nobreak >nul
  echo ---- running cache_%%C
  docker compose --profile loadtest run --rm locust --headless -u 10 -r 2 -t 90s --stop-timeout 120 --host http://api:8000 --csv /mnt/locust/results/cache_%%C --only-summary >nul 2>&1
  timeout /t 10 /nobreak >nul
  docker compose --profile loadtest run --rm --entrypoint python locust /mnt/locust/summarize.py cache cache_%%C %%C
)
echo.
docker compose --profile loadtest run --rm --entrypoint python locust /mnt/locust/summarize.py table
rem restore the normal configuration
if exist .env.before-loadtest (copy /y .env.before-loadtest .env >nul & del .env.before-loadtest) else (del .env)
docker compose up -d
echo.
echo DONE. Results are in loadtest\results  (matrix.md, matrix.json, cache_experiment.md, Locust CSVs)
pause
