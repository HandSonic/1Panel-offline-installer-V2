ARG GO_VERSION=1.26.1
ARG NODE_VERSION=22.14.0
ARG VERSION=v2.3.2

FROM node:${NODE_VERSION}-bookworm AS frontend-builder
ARG VERSION
ARG NODE_VERSION
ARG SOURCE_COMMIT=65243c68c463cc055ab044093f641ea5d2e9e28b
ENV NODE_OPTIONS=--max-old-space-size=8192
COPY scripts /opt/build-tools/scripts
COPY config /opt/build-tools/config
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git python3 && rm -rf /var/lib/apt/lists/*
RUN set -eu; eval "$(python3 /opt/build-tools/scripts/resolve_inputs.py "$VERSION")"; \
    test "$(node --version)" = "v$NODE_VERSION"; test "$(npm --version)" = "$NPM_VERSION"; \
    git init /src; cd /src; git remote add origin https://github.com/1Panel-dev/1Panel.git; \
    git fetch --depth=1 origin "$SOURCE_COMMIT"; git checkout --detach FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"
WORKDIR /src/frontend
RUN node /opt/build-tools/scripts/patch_backend_xpack_compat.mjs /src \
    && node /opt/build-tools/scripts/patch_frontend_xpack_compat.mjs /src/frontend \
    && npm ci --no-audit --no-fund \
    && npm run build:pro \
    && test -s /src/core/cmd/server/web/index.html \
    && rm -rf node_modules /root/.npm

FROM golang:${GO_VERSION} AS builder
ARG VERSION
ARG GO_VERSION
ARG INSTALLER_REF=aa4a6bbf24ae0fd938b32294672f5086f940e483
ARG TARGET_ARCHES="amd64 arm64 armv7 ppc64le s390x loong64 riscv64"
ARG BUILD_REPOSITORY_COMMIT
ENV VERSION=${VERSION} INSTALLER_REF=${INSTALLER_REF} TARGET_ARCHES=${TARGET_ARCHES}
ENV BUILD_REPOSITORY_COMMIT=${BUILD_REPOSITORY_COMMIT} GOTOOLCHAIN=local
COPY scripts /opt/build-tools/scripts
COPY config /opt/build-tools/config
COPY --from=frontend-builder /src /opt/1Panel
WORKDIR /opt/1Panel
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3 && rm -rf /var/lib/apt/lists/*
RUN set -eu; eval "$(python3 /opt/build-tools/scripts/resolve_inputs.py "$VERSION")"; \
    test "$(go env GOVERSION)" = "go$GO_VERSION"; \
    python3 /opt/build-tools/scripts/configure_release.py /opt/1Panel "$VERSION"; \
    /bin/bash /opt/build-tools/scripts/download_resources.sh; \
    sed -i "s@^ORIGINAL_VERSION=.*@ORIGINAL_VERSION=${VERSION}@" 1pctl
RUN /bin/bash /opt/build-tools/scripts/build_release.sh

FROM debian:bookworm-slim
WORKDIR /opt/1Panel
COPY --from=builder /opt/1Panel/dist /opt/1Panel/dist
# The exporting container may run as the unprivileged host runner UID.
RUN chmod 755 /opt/1Panel/dist && chmod 644 /opt/1Panel/dist/*
VOLUME /dist
# Copy files only: cp -a dist/. also changes the bind mount directory ownership,
# preventing an unprivileged CI runner from adding provenance after export.
CMD ["/bin/sh", "-c", "cp dist/* /dist/"]
