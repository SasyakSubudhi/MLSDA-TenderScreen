# One image for every Python service (api, agent-worker, verifier-worker,
# llm-gateway, llm-stub, indexer); each service runs a different command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tenderscreen ./tenderscreen
COPY corpus ./corpus
COPY prompts ./prompts
COPY examples ./examples
COPY eval ./eval

# run as an unprivileged user; /data is the shared index/model volume
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data/index /data/models \
 && chown -R app:app /data /app
USER app

CMD ["python", "-m", "tenderscreen.agents.worker"]
