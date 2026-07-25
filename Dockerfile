FROM node:24-alpine AS frontend

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run check-build

FROM python:3.12-slim AS runtime

ARG AGENT_MEMORY_BUILD_VERSION=dev
ARG AGENT_MEMORY_BUILD_REVISION=unknown
LABEL org.opencontainers.image.title="Agent Memory for Hermes" \
      org.opencontainers.image.version="${AGENT_MEMORY_BUILD_VERSION}" \
      org.opencontainers.image.revision="${AGENT_MEMORY_BUILD_REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.14

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra migrations --no-install-project

COPY src ./src
COPY --from=frontend /frontend/dist ./src/agent_memory/static
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --extra migrations --no-editable

RUN useradd --create-home --uid 10001 agent-memory
USER agent-memory

ENV PATH="/app/.venv/bin:${PATH}"

CMD ["agent-memory-api"]
