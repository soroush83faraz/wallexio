# wallexbot, SHADOW mode only. Live trading does not exist in this build; no secrets are baked in.
# The base tag is pinned; bump it deliberately (and re-run the tests) rather than floating.
FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Editable install on purpose: config.DEFAULT_PATH is resolved relative to the source tree
# (<root>/config/default.yaml), and `guardian` has no --config flag, so the package must run from /app.
COPY pyproject.toml ./
COPY src ./src
COPY config ./config
COPY deploy/healthcheck.py deploy/run_recorder.py ./deploy/
RUN pip install -e . \
    && python -m compileall -q src deploy

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
