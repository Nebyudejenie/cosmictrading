# syntax=docker/dockerfile:1
# COSMICTRADING QUANT application image: web, worker and bot run from this one image.
# Base images are pinned by digest (resolved 2026-10-11); update deliberately, never implicitly.
# Build from the repository root:  docker build -f deploy/Dockerfile -t cosmictrading:<version> .

ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first (cached layer), exactly as locked; no dev tools in the image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
 && find /opt/venv -name "*.pyc" -path "*/tests/*" -delete

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="cosmictrading-quant" \
      org.opencontainers.image.description="Evidence-driven crypto research with a human-approved paper trading engine"
# The official image ships the standard library without .pyc files; with a read-only root
# filesystem every process start (including each healthcheck) would recompile it.
RUN python -m compileall -q -j 0 /usr/local/lib/python3.12 \
 && groupadd --gid 10001 cosmic \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin cosmic
COPY --from=build /opt/venv /opt/venv
COPY config /app/config
WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COSMIC_AI_REGISTRY_PATH=/app/config/ai_registry.toml \
    COSMIC_WEB_HOST=0.0.0.0 \
    COSMIC_WEB_PORT=8000
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["cosmic"]
CMD ["--help"]
