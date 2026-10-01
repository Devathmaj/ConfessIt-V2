# Dev-only image for the ConfessIt SPA (HMR via bind mount; see docker-compose.yml).
# Production uses Dockerfile.prod (multi-stage npm ci + build + nginx:alpine, USER nginx).
# Use an official Node.js runtime as a parent image (22: the minimum line Vite 7 supports)
FROM node:22-slim

# Set the working directory in the container
WORKDIR /app

# Copy package.json and package-lock.json only (bun.lockb is not consumed by npm and
# must not invalidate the layer or shadow the npm lockfile).
COPY package.json package-lock.json ./

# Reproducible dev install from the lockfile (not `npm install`, which can drift it).
RUN npm ci

# Copy the rest of the application's code
COPY . .

# The Vite dev server listens on 8080 (see vite.config.ts); compose publishes it as
# 127.0.0.1:5173:8080, so EXPOSE names the container port, not the host one.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8080/').then(()=>process.exit(0)).catch(()=>process.exit(1))"

# Command to run the application
CMD ["npm", "run", "dev"]
