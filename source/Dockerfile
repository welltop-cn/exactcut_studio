FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg antiword procps libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/source
COPY source/requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY source/exactcut ./exactcut
COPY source/config ./config
COPY source/templates ./templates
COPY integrations/drama-material-hub/scripts /app/integrations/drama-material-hub/scripts
COPY source/docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

ARG EXACTCUT_COMMIT=unversioned
LABEL org.opencontainers.image.revision=$EXACTCUT_COMMIT
LABEL io.exactcut.stage-protocol="1"
LABEL io.exactcut.media-protocol="1"
LABEL io.exactcut.editor-protocol="1"

ENV PYTHONPATH=/app/source \
    PYTHONUNBUFFERED=1 \
    EXACTCUT_DATA_ROOT=/data \
    EXACTCUT_ENVIRONMENT=development \
    EXACTCUT_ALLOW_EXTERNAL_WRITES=0 \
    EXACTCUT_WORKER_ENABLED=0 \
    EXACTCUT_ALLOW_MATERIAL_FETCH=0 \
    EXACTCUT_ALLOW_PUBLISH=0 \
    EXACTCUT_ALLOW_AI=0 \
    EXACTCUT_PORT=8792 \
    EXACTCUT_BIND=0.0.0.0 \
    EXACTCUT_RELEASE_ID=development

EXPOSE 8792
ENTRYPOINT ["/entrypoint.sh"]
