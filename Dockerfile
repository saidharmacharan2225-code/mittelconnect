# syntax=docker/dockerfile:1.7
# =============================================================================
# MittelConnect middleware image
#   Stage 1 (builder): builds a self-contained virtualenv with all wheels.
#   Stage 2 (runtime): slim Debian 12 + Microsoft ODBC Driver 18, no compilers,
#                      runs as an unprivileged user with a read-only filesystem.
# Build:  docker build -t mittelconnect:1.0.0 .
# =============================================================================

ARG PYTHON_IMAGE=python:3.11-slim-bookworm

# ----------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential unixodbc-dev \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
 && python -c "import yaml, cryptography, httpx, oracledb, tzdata" \
 && pip uninstall -y setuptools wheel

# ----------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

LABEL org.opencontainers.image.title="MittelConnect" \
      org.opencontainers.image.description="Legacy DB to SAP S/4HANA middleware" \
      org.opencontainers.image.version="1.0.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    MC_CONFIG=/app/config/config.yaml \
    MC_CACHE_PATH=/app/data/mittelconnect_cache.db \
    MC_HEARTBEAT_FILE=/app/data/heartbeat \
    MC_MASTER_KEY_FILE=/run/secrets/mittelconnect_master.key \
    MC_HEALTH_MAX_AGE_SECONDS=600

# Microsoft ODBC Driver 18 for SQL Server (signed Microsoft apt repository).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
    > /etc/apt/sources.list.d/microsoft-prod.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

RUN groupadd --system --gid ${APP_GID} mittelconnect \
 && useradd --system --uid ${APP_UID} --gid ${APP_GID} --home-dir /app \
    --no-create-home --shell /usr/sbin/nologin mittelconnect

COPY --from=builder /opt/venv /opt/venv

# The service never installs packages at runtime; drop the base image's
# build tooling (and the libraries it vendors) from the attack surface.
# Explicit path: "python" on PATH is already the venv interpreter here.
RUN /usr/local/bin/python -m pip uninstall -y setuptools wheel

WORKDIR /app
COPY --chown=root:root main.py ./
COPY --chown=root:root core/ ./core/
COPY --chown=root:root config.yaml ./config/config.yaml

# Only the data directory is writable by the service user.
RUN mkdir -p /app/data \
 && chown ${APP_UID}:${APP_GID} /app/data \
 && chmod 0700 /app/data \
 && python -m compileall -q /app/core /app/main.py

USER ${APP_UID}:${APP_GID}

VOLUME ["/app/data"]

# Healthy while a pipeline cycle finished within MC_HEALTH_MAX_AGE_SECONDS.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import os,sys,time; p=os.environ['MC_HEARTBEAT_FILE']; m=float(os.environ['MC_HEALTH_MAX_AGE_SECONDS']); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < m else 1)"]

# Exec form: Python is PID 1 and receives SIGTERM directly for graceful shutdown.
ENTRYPOINT ["python", "/app/main.py"]
CMD ["run"]
