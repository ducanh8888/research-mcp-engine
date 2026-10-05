FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.17@sha256:5cb6b54d2bc3fe2eb9a8483db958a0b9eebf9edff68adedb369df8e7b98711a2 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RESEARCH_ENGINE_CONFIG=/app/config.yaml \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY scripts/mcp_smoke.py ./scripts/mcp_smoke.py
RUN groupadd --gid 1000 research \
    && useradd --uid 1000 --gid research --no-create-home research \
    && mkdir -p /app/data \
    && chown research:research /app/data

USER research:research
EXPOSE 8765
CMD ["research-engine", "serve", "--config", "/app/config.yaml", "--host", "0.0.0.0", "--port", "8765"]
