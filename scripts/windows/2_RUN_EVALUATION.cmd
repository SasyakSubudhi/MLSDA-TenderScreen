@echo off
title TenderScreen - evaluation (28 scenarios, prompt comparison, deliberate change, TruLens)
cd /d "%~dp0..\.."
call "%~dp0_wait_ready.cmd" || (echo Start the system first with 1_START_SYSTEM.cmd & pause & exit /b 1)
set RUN=docker compose exec -T agent-worker python -m tenderscreen.evaluation
echo [1/8] prompt v1 vs v2 on the 28 scenarios
%RUN%.run --compare v1 v2 --out results || goto fail
echo [2/8] deliberate change: top-k 4 to 2
%RUN%.run --set RETRIEVAL_K=2 --label v2_k2 --out results || goto fail
echo [3/8] faulty model: every first draft cites an unretrieved passage
%RUN%.run --demo-fault unsupported_citation --label v2_faulty_model --out results || goto fail
echo [4/8] TruLens: v1
%RUN%.trulens_eval --app-version v1 --prompt-version v1 --db results/trulens.sqlite --out results || goto fail
echo [5/8] TruLens: v2
%RUN%.trulens_eval --app-version v2 --db results/trulens.sqlite --out results || goto fail
echo [6/8] TruLens: v2 with top-k 2
%RUN%.trulens_eval --app-version v2_k2 --set RETRIEVAL_K=2 --db results/trulens.sqlite --out results || goto fail
echo [7/8] TruLens: faulty model
%RUN%.trulens_eval --app-version v2_faulty_model --demo-fault unsupported_citation --db results/trulens.sqlite --out results || goto fail
echo [8/8] TruLens leaderboard
%RUN%.trulens_eval --leaderboard --db results/trulens.sqlite --out results || goto fail
docker compose cp agent-worker:/app/results/. eval\results
echo.
echo DONE. Results are in eval\results  (eval_*.json, trulens.sqlite, trulens_leaderboard.json)
pause
exit /b 0
:fail
echo Evaluation step failed - see the messages above.
pause
exit /b 1
