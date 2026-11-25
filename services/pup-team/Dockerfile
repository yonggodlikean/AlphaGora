# The OpenPup team runner. OpenPup itself is NOT in this image: deploy/fetch-openpup.sh installs the release
# binary on the host under /opt/openpup, and docker-compose mounts it read-only. Build context: repository root.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10002 --create-home --home-dir /data pup
COPY packages/openpup-skill /app
COPY services/pup-team/entrypoint.py /app/entrypoint.py
ENV OPENPUP_BIN=/opt/openpup/current/openpup OPENPUP_HOME=/data HOME=/data PYTHONUNBUFFERED=1
USER pup
VOLUME /data
ENTRYPOINT ["python3", "/app/entrypoint.py"]
