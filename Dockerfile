FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.14-slim

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The analysis engine is the project's own package; its metadata includes
# the README, without which the package does not build.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

RUN useradd --create-home invariant && chown -R invariant:invariant /app
USER invariant

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
