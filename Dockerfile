FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RESEARCH_ENGINE_CONFIG=/app/config.yaml

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd --gid 1000 research \
    && useradd --uid 1000 --gid research --no-create-home research \
    && mkdir -p /app/data \
    && chown research:research /app/data

COPY scripts/mcp_smoke.py ./scripts/mcp_smoke.py
USER research:research
EXPOSE 8765
CMD ["research-engine", "serve", "--config", "/app/config.yaml", "--host", "0.0.0.0", "--port", "8765"]
