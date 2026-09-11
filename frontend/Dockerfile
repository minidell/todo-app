# syntax=docker/dockerfile:1
# Build context is the repository root (see docker-compose.yml `build.context: .`)
# so this stage can reach docker/nginx.conf as well as frontend/.

FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
