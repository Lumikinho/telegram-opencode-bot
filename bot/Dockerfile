FROM oven/bun:1 AS runtime
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production
COPY src/ ./src/
ENV APP_ENV=prod
CMD ["bun", "src/index.ts"]
