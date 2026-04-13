FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./

RUN mkdir -p chronos_agent && touch chronos_agent/__init__.py

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e .

COPY chronos_agent/ ./chronos_agent/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY scripts/ ./scripts/

RUN mkdir -p /app/audio

CMD ["sh", "-c", "alembic upgrade head && uvicorn chronos_agent.main:app --host 0.0.0.0 --port 8000 --access-log"]
