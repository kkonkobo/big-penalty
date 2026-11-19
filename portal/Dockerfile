# Big Penalty portal server. Build context: the repository root (portal/Dockerfile.dockerignore whitelists the
# few files that enter it).   docker build -f portal/Dockerfile --build-arg GIT_SHA=<commit> .
# The image holds no secret: every key arrives as a runtime environment variable (see portal/README.md).

# python:3.13-slim, resolved on the Docker Hub registry on 2026-11-18. To refresh, read the new digest of the tag
# (Docker-Content-Digest of the manifest list) and update both stages.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Stage 1: install the hash-pinned dependencies into a virtualenv. Binary wheels only, so no compiler is needed
# and none ends up in the final image.
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY portal/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /tmp/requirements.txt

# Stage 2: runtime.
FROM ${PYTHON_IMAGE}
ARG GIT_SHA=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv/app \
    PATH=/opt/venv/bin:$PATH \
    PORTAL_VERSION=$GIT_SHA

RUN groupadd --gid 10001 portal \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin portal \
 && install -d -m 0700 -o 10001 -g 10001 /data

COPY --from=build /opt/venv /opt/venv

# Exactly what the server imports or serves, nothing else of the repository.
COPY src/jobhunt/__init__.py /srv/app/jobhunt/__init__.py
COPY src/jobhunt/portal/__init__.py src/jobhunt/portal/protocol.py /srv/app/jobhunt/portal/
COPY src/jobhunt/portal/server/ /srv/app/jobhunt/portal/server/
COPY src/jobhunt/web/static/style.css /srv/app/jobhunt/web/static/style.css
# Code stays owned by root and read-only to the service user.
RUN chmod -R u=rwX,go=rX /srv/app

WORKDIR /srv/app
USER 10001:10001
EXPOSE 8080
# Standard library only (no curl or wget in the slim image). The server refuses a Host other than its public one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.parse as p, urllib.request as r; r.urlopen(r.Request('http://127.0.0.1:8080' + os.environ.get('PORTAL_BASE_PATH', '/app').rstrip('/') + '/healthz', headers={'Host': p.urlsplit(os.environ['PORTAL_PUBLIC_ORIGIN']).netloc}), timeout=3).read()"]
ENTRYPOINT ["python", "-m", "jobhunt.portal.server"]
