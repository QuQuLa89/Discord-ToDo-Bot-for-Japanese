FROM python:3.10-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install "poetry>=2.0.0,<3.0.0"

COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root --no-interaction --no-ansi

COPY main.py ./
COPY todo_bot ./todo_bot

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p data logs backups \
    && chown -R appuser:appuser /app
USER appuser

VOLUME ["/app/data", "/app/logs", "/app/backups"]

CMD ["python", "main.py"]
