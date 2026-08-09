FROM python:3.11-slim AS dependency-base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM dependency-base AS orchestrator

COPY app.py .
COPY webvault/ ./webvault/
COPY img/ ./img/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]


FROM dependency-base AS worker

RUN apt-get update && apt-get install -y --no-install-recommends docker-cli ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY app.py .
COPY webvault/ ./webvault/
COPY img/ ./img/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "webvault.worker"]
