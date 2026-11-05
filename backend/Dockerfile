FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy UV_NO_DEV=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-editable
COPY gl_bot ./gl_bot
COPY gl_api ./gl_api
RUN uv sync --locked --no-editable

FROM python:3.12-slim AS runtime

RUN groupadd --gid 10001 glbot \
    && useradd --uid 10001 --gid glbot --home-dir /app --no-create-home glbot
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 3000
CMD ["python", "-m", "gl_bot.server"]

FROM build AS test

ENV UV_NO_DEV=0
RUN uv sync --locked --extra test --no-editable \
    && groupadd --gid 10001 glbot \
    && useradd --uid 10001 --gid glbot --home-dir /app --no-create-home glbot
COPY tests ./tests
COPY sql ./sql
COPY migrations ./migrations
COPY infra ./infra
COPY alembic.ini ./
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
