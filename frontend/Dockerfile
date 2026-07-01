FROM cgr.dev/chainguard/node:latest-dev AS build

# Chainguard's node image defaults to a non-root user; WORKDIR /app is created
# owned by root, so npm can't write package-lock.json. Run the throwaway build
# stage as root. The runtime nginx stage below stays non-root.
USER root

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --frozen-lockfile 2>/dev/null || npm install

COPY . .
RUN npm run build

FROM cgr.dev/chainguard/nginx:latest

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 2273
