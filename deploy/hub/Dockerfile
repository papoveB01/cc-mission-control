# cc-mission-control history hub. Build context is the REPO ROOT:
#   docker build -f deploy/hub/Dockerfile .
# The root .dockerignore is an allowlist (pyproject, README, LICENSE, NOTICE, package).

FROM python:3.12.14-slim AS builder
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY cc_mission_control ./cc_mission_control
RUN pip wheel --no-cache-dir --no-deps -w /wheels .

FROM python:3.12.14-slim
ARG VERSION=0.3.0
LABEL org.opencontainers.image.title="cc-mission-control-hub" \
      org.opencontainers.image.description="Remote history hub for cc-mission-control" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/papoveB01/cc-mission-control" \
      org.opencontainers.image.licenses="Elastic-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CCMC_HUB_DB=/data/history.db
COPY deploy/hub/requirements.lock /tmp/requirements.lock
COPY --from=builder /wheels /wheels
# Dependencies are pinned by requirements.lock (uv export of uv.lock); the wheel adds only the app.
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
 && pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /wheels /tmp/requirements.lock \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin ccmc \
 && mkdir /data && chown 10001:10001 /data
USER 10001
VOLUME /data
EXPOSE 4318
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4318/health', timeout=4).status == 200 else 1)"]
CMD ["ccmc-hub"]
