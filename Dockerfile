# todobot for Railway: `serve` on $PORT; `python -m todobot migrate` runs as railway.json's preDeployCommand.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# The locked runtime dependencies first: this layer is rebuilt only when uv.lock changes.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

# Then the app itself, installed into the same environment.
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

RUN useradd --system --no-create-home --uid 10001 todobot
USER todobot

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8080
EXPOSE 8080

CMD ["python", "-m", "todobot", "serve"]
