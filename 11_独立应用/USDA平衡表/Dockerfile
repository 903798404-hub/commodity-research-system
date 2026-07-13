FROM node:24-alpine AS build

WORKDIR /app

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

COPY . .

ARG VITE_BASE_PATH=/usda/
ENV VITE_BASE_PATH=${VITE_BASE_PATH}
RUN pnpm run build

FROM nginx:1.27-alpine

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html/usda

EXPOSE 80
