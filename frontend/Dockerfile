FROM node:24-alpine

ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup -S app && adduser -S app -G app

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN mkdir -p /app/.next && chown -R app:app /app

USER app

EXPOSE 3000

CMD ["npm", "run", "dev"]
