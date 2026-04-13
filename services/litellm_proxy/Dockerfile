FROM ghcr.io/berriai/litellm:main-latest

WORKDIR /app

COPY config.yaml .
COPY guardrails_hook.py .

ENV PYTHONPATH=/app