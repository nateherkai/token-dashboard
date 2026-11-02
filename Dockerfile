FROM python:3.12-slim

WORKDIR /app

# Copy the local heliosdb-sqlite wheel BEFORE pip install so the file://
# reference in requirements.txt resolves at install time.
COPY wheels/ /app/wheels/
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

ENV HOST=0.0.0.0 \
    PORT=8080 \
    CLAUDE_PROJECTS_DIR=/data/projects \
    TOKEN_DASHBOARD_DB=/data/cache/token-dashboard.db \
    PYTHONUNBUFFERED=1
# Backend selection: leave TOKEN_DASHBOARD_BACKEND unset for auto-detect
# (heliosdb_sqlite is now installed, so it wins). Set HELIOSDB_DSN to opt
# into daemon mode against a running heliosdb-nano server, e.g.
#   HELIOSDB_DSN=postgresql://user:pass@helios:5432/token_dashboard
# Embedded mode requires the heliosdb-nano binary on PATH (or override via
# HELIOSDB_BIN) — install separately or mount from the host.

RUN groupadd --gid 1001 token-dashboard \
    && useradd --uid 1001 --gid 1001 --create-home \
        --home-dir /home/token-dashboard --shell /usr/sbin/nologin token-dashboard \
    && mkdir -p /data/cache \
    && chown 1001:1001 /data/cache

USER 1001:1001

EXPOSE 8080

CMD ["python3", "cli.py", "dashboard", "--no-open"]
