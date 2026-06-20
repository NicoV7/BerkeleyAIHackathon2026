FROM node:22-slim

RUN corepack enable

WORKDIR /app

# The compose mount supplies source; install happens against the mounted pkg.
COPY package.json ./
RUN npm install --no-package-lock || true

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
