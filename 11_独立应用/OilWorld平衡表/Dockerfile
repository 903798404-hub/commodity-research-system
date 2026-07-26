FROM node:24-alpine AS build

WORKDIR /workspace/05_apps/oil_world_dashboard

ARG MARKET_DATA_GIT_HEAD
ARG MARKET_DATA_GIT_TREE
ARG RELEASE_ID

COPY 05_apps/oil_world_dashboard/package.json \
     05_apps/oil_world_dashboard/pnpm-lock.yaml \
     05_apps/oil_world_dashboard/pnpm-workspace.yaml ./

RUN corepack enable && pnpm --version && pnpm install --frozen-lockfile

COPY 05_apps/oil_world_dashboard/ ./

# vite.config.ts resolves the public directory at /workspace/public. Production
# data is intentionally not copied into the image; it is mounted read-only.
RUN mkdir -p /workspace/public

ARG OIL_WORLD_BASE_PATH=/oil-world/
ENV OIL_WORLD_BASE_PATH=${OIL_WORLD_BASE_PATH}

RUN pnpm run build \
    && test ! -e dist/data/oil_world

# Release identity is deliberately built into the static image.  The public
# Oil World data remains an external read-only runtime mount.
RUN test "${MARKET_DATA_GIT_HEAD}" != "" \
    && test "${MARKET_DATA_GIT_TREE}" != "" \
    && test "${RELEASE_ID}" != "" \
    && node -e "const fs=require('fs'); fs.writeFileSync('/workspace/RELEASE.json', JSON.stringify({git_commit:process.env.MARKET_DATA_GIT_HEAD,git_tree:process.env.MARKET_DATA_GIT_TREE,release_id:process.env.RELEASE_ID}, null, 2)+'\\n')"

FROM nginx:1.27-alpine

ARG MARKET_DATA_GIT_HEAD
ARG MARKET_DATA_GIT_TREE
ARG RELEASE_ID

LABEL org.opencontainers.image.revision="${MARKET_DATA_GIT_HEAD}" \
      org.opencontainers.image.version="${RELEASE_ID}" \
      org.opencontainers.image.title="market-data-oil-world-dashboard"

ENV MARKET_DATA_GIT_HEAD="${MARKET_DATA_GIT_HEAD}" \
    MARKET_DATA_GIT_TREE="${MARKET_DATA_GIT_TREE}" \
    MARKET_DATA_RELEASE_ID="${RELEASE_ID}"

RUN rm -rf /usr/share/nginx/html/* \
    && mkdir -p /usr/share/nginx/html/oil-world/data/oil_world

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/healthcheck.sh /usr/local/bin/oil-world-healthcheck
COPY --from=build /workspace/05_apps/oil_world_dashboard/dist /usr/share/nginx/html/oil-world
COPY --from=build /workspace/RELEASE.json /usr/share/nginx/html/oil-world/RELEASE.json

RUN chmod +x /usr/local/bin/oil-world-healthcheck

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/usr/local/bin/oil-world-healthcheck"]
