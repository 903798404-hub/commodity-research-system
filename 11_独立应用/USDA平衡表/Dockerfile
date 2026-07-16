FROM node:24-alpine AS build

WORKDIR /app

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

COPY . .

ARG VITE_BASE_PATH=/usda/
ENV VITE_BASE_PATH=${VITE_BASE_PATH}
RUN pnpm run build

FROM nginx:1.27-alpine

ARG OCI_REVISION=unknown
ARG OCI_SOURCE=https://github.com/903798404-hub/commodity-research-system
ARG OCI_CREATED=unknown

LABEL org.opencontainers.image.revision=${OCI_REVISION} \
      org.opencontainers.image.source=${OCI_SOURCE} \
      org.opencontainers.image.created=${OCI_CREATED}

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html/usda

EXPOSE 80
