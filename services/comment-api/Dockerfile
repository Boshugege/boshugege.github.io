FROM docker.io/library/node:22-alpine3.22@sha256:8d6a8a2f25f8950664594b4c64678909718af0f567c99a85e44b11d0b44e059d

WORKDIR /app
COPY services/comment-api/package.json services/comment-api/server.mjs ./
USER node
EXPOSE 8080
CMD ["node", "/app/server.mjs"]
