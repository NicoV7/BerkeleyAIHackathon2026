# Build context is the REPO ROOT (see docker-compose) so the pnpm workspace
# (@debate/shared) resolves. Dev-server image with hot reload via bind mounts.
FROM node:22-slim

RUN corepack enable

WORKDIR /repo

# Install workspace deps using just the manifests first (layer caching).
COPY pnpm-workspace.yaml package.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/shared/package.json packages/shared/package.json
RUN pnpm install

# Copy the rest (overridden by bind mounts at runtime for hot reload).
COPY . .

WORKDIR /repo/apps/web
EXPOSE 5173
CMD ["pnpm", "dev", "--host", "0.0.0.0"]
