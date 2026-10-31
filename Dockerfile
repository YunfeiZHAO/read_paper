FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=latest
RUN npm install --global "@openai/codex@${CODEX_VERSION}"

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /usr/local/bin/
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && apt-get update \
    && apt-get install --no-install-recommends -y bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/instance \
    && chown app:app /app/instance

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    CODEX_QA_DATA_DIR=/app/instance

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra codex --extra tools

COPY app.py ./
COPY app/ ./app/
COPY static/ ./static/
COPY templates/ ./templates/

USER app
EXPOSE 8080
CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
