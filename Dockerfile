# Static hosting for the precomputed site in public/.
# Railway runs this container and routes traffic to Caddy on $PORT.
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY public/ /srv/
