# wallexbot, SHADOW mode only (simulated fills on the public order book; no orders are ever sent).
# The base tag is pinned; bump it deliberately (and re-run the tests) rather than floating.
# Docker Hub is blocked on some networks: build through a registry mirror with
#   docker build --build-arg BASE_IMAGE=<mirror>/library/python:3.12.11-slim-bookworm -t wallexbot:shadow .
ARG BASE_IMAGE=python:3.12.11-slim-bookworm
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Regular (non-editable) install into site-packages; the default config ships inside the package
# (wallexbot/default_config.yaml), so no source tree is needed at run time. The build tree is removed again.
COPY pyproject.toml /build/pyproject.toml
COPY src /build/src
RUN pip install /build \
    && rm -rf /build

WORKDIR /app
COPY deploy/healthcheck.py ./deploy/

# Unprivileged user; /data is the only writable place (SQLite DB, shadow journal, recorder output).
RUN groupadd --system --gid 10001 wallex \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin wallex \
    && mkdir /data \
    && chown 10001:10001 /data
VOLUME ["/data"]
USER 10001:10001

# Healthy = fresh strategy heartbeat (and in-process guardian heartbeat when enabled), kill switch NOT engaged
# (an engaged switch fails the probe by default), no recent guardian alerts (read-only look at the DB).
# The first heartbeat appears after the bot's first tick, hence the start period.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "/app/deploy/healthcheck.py", "--db", "/data/bot.db"]

# --interval-s must be <= max_data_age_s / 2 (15 s): the feed refreshes only on bot ticks.
CMD ["python", "-m", "wallexbot", "bot", "--db", "/data/bot.db", "--mode", "shadow", "--balance", "1000", "--interval-s", "12"]
