# Build context: repository root (needs db/migrations).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ARENA_MIGRATIONS_DIR=/app/db/migrations
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY services/arena-engine/pyproject.toml ./
COPY services/arena-engine/src ./src
RUN uv pip install --system --no-cache ".[api]"
COPY db/migrations ./db/migrations
RUN useradd --system --uid 10001 arena
USER arena
EXPOSE 8000
# Only reachable from the compose network (web, pup-team): trust their X-Forwarded-For for per-visitor limits.
CMD ["uvicorn", "arena_engine.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
