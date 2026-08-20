FROM python:3.13-slim

# No third-party runtime dependencies: everything the bot needs is in the standard library.
# That is a deliberate choice — it means a redeploy in six months cannot be broken by a package
# that changed under us, and there is no dependency tree to audit before trusting a number.

WORKDIR /app
COPY csmbot/ ./csmbot/
COPY README.md ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CSM_DB_PATH=/data/csm.db

RUN useradd --create-home --uid 10001 csmbot && mkdir -p /data && chown csmbot:csmbot /data
USER csmbot

VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD ["python", "-m", "csmbot.main", "health"]

CMD ["python", "-m", "csmbot.main", "run"]
