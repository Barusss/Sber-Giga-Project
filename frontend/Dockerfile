FROM node:22-alpine
WORKDIR /code

# Use pinned versions and cached tarballs across Docker builds.
COPY package.json package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund \
    --registry="${NPM_REGISTRY}" \
    --fetch-retries=2 --fetch-timeout=120000 \
    --fetch-retry-mintimeout=5000 --fetch-retry-maxtimeout=20000

COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
