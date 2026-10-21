# Production image for the ConfessIt SPA: build once, serve static files with Nginx.
# Development uses Dockerfile.dev (Vite dev server over a bind mount).
#
# VITE_API_URL is baked in at build time and enforced to be https:// by
# `npm run build:prod` -- a build without it fails instead of shipping a bundle
# that guesses its API. Pass it as a build arg (see docker-compose.yml).
# The running container needs no Node, no source and no secrets: only the dist
# output, and SERVER_NAME/API_ORIGIN for the Nginx template below.
FROM node:22-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build:prod

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
# Rendered at container start from SERVER_NAME/API_ORIGIN (nginx:alpine envsubst).
COPY nginx.prod.conf.example /etc/nginx/templates/default.conf.template
EXPOSE 80 443
# Drop privileges: serve static files as nginx, not root. The dist output and the
# rendered template must be world-readable for this to hold (they are: COPY defaults).
USER nginx
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
