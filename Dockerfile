FROM python:3.12-slim AS base

# System deps:
#   - curl, ca-certificates: for installing uv and Node
#   - git: claude CLI shells out to git for some operations
#   - tini: PID 1 signal handling so SIGTERM from compose stops cleanly
#   - poppler-utils: provides pdftoppm, which the claude CLI uses to render
#     PDF pages into images when reading PDFs
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates git tini poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (for the claude CLI). NodeSource setup script.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# claude CLI — installed globally via npm, version PINNED. An unpinned
# install pulls latest on every rebuild; CLI behavior changes there have
# repeatedly broken Ember's MCP bridge integration (stateless-session tool
# registration, ToolSearch deferral). Bump deliberately via
# scripts/upgrade-claude.sh, which rebuilds and smoke-tests before keeping it.
ARG CLAUDE_CODE_VERSION=2.1.296
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# Sandbox libraries for the agent's own `python3` (Pillow, numpy, openpyxl,
# xlsx2csv) are NOT installed here: the claude subprocess inherits PATH with
# /app/.venv/bin first, so a system-site install is invisible to it. They
# come in through the `sandbox` extra of `uv sync` below. For legacy
# .xls/.ods, LibreOffice headless would be needed, but that's a ~400 MB image
# hit not worth it for rare formats.

# uv (single static binary).
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /uvx /usr/local/bin/

# Non-root user (uid 1000, matches host). Claude CLI refuses
# --permission-mode bypassPermissions under root; running as a regular
# user is the cleanest fix.
RUN groupadd --gid 1000 ember \
    && useradd  --uid 1000 --gid 1000 --create-home --shell /bin/bash ember

# claude reads/writes auth state at $HOME/.claude — set HOME to the mounted
# volume so a `claude login` survives container restarts.
# PIP_REQUIRE_VIRTUALENV: the system `pip` on PATH would otherwise fall back
# to a `--user` install under /data/.local — which persists across restarts,
# grew to 750 MB of orphaned wheels, and is invisible to the venv python the
# agent actually runs. Make `pip install` fail loudly instead.
ENV HOME=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PIP_REQUIRE_VIRTUALENV=true

WORKDIR /app

# Install Python deps in their own layer for build cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra sandbox

# Project source.
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev --extra sandbox

# Build-time stamp of the source revision, surfaced at runtime as
# EMBER_IMAGE_VERSION — the "ember image version" component of the eval-harness
# config fingerprint (v11, FC-21). Passed from compose as
# `--build-arg EMBER_GIT_SHA=$(git rev-parse --short HEAD)`; defaults to
# "unknown" for a bare `docker build` without the arg (fingerprint then falls
# back to the pinned claude CLI version). Placed after the source copy so it
# only invalidates the final cheap layers, never the dependency cache.
ARG EMBER_GIT_SHA=unknown
ENV EMBER_IMAGE_VERSION=${EMBER_GIT_SHA}

# Hand the build artefacts over to the runtime user so `uv run` can
# write bytecode caches in /app at runtime.
RUN chown -R 1000:1000 /app

USER 1000:1000

ENTRYPOINT ["/usr/bin/tini", "--", "uv", "run", "ember"]
