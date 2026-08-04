FROM node:24-alpine AS base

ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup -S app && adduser -S app -G app

WORKDIR /app

FROM base AS dependencies

COPY package.json package-lock.json ./
RUN npm ci

FROM dependencies AS development

COPY --chown=app:app . .

USER app

EXPOSE 3000

CMD ["npm", "run", "dev"]

FROM dependencies AS builder

ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}

RUN test -n "$NEXT_PUBLIC_API_URL"

COPY . .
RUN npm run build

FROM base AS production-dependencies

ENV NODE_ENV=production

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

FROM base AS production

ENV NODE_ENV=production

COPY --from=production-dependencies --chown=app:app /app/node_modules ./node_modules
COPY --from=builder --chown=app:app /app/.next ./.next
COPY --from=builder --chown=app:app /app/public ./public
COPY --from=builder --chown=app:app /app/package.json /app/package-lock.json /app/next.config.ts ./

USER app

EXPOSE 3000

CMD ["npm", "run", "start"]
