FROM node:24-alpine AS build

WORKDIR /workspace/05_apps/oil_world_dashboard

COPY 05_apps/oil_world_dashboard/package.json \
     05_apps/oil_world_dashboard/pnpm-lock.yaml \
     05_apps/oil_world_dashboard/pnpm-workspace.yaml ./

RUN corepack enable && pnpm install --frozen-lockfile

COPY 05_apps/oil_world_dashboard/ ./

# vite.config.ts resolves the public directory at /workspace/public. Production
# data is intentionally not copied into the image; it is mounted read-only.
RUN mkdir -p /workspace/public

ARG OIL_WORLD_BASE_PATH=/oil-world/
ENV OIL_WORLD_BASE_PATH=${OIL_WORLD_BASE_PATH}

RUN pnpm run build \
    && test ! -e dist/data/oil_world

FROM nginx:1.27-alpine

RUN rm -rf /usr/share/nginx/html/* \
    && mkdir -p /usr/share/nginx/html/oil-world/data/oil_world

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/healthcheck.sh /usr/local/bin/oil-world-healthcheck
COPY --from=build /workspace/05_apps/oil_world_dashboard/dist /usr/share/nginx/html/oil-world

RUN chmod +x /usr/local/bin/oil-world-healthcheck

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/usr/local/bin/oil-world-healthcheck"]
