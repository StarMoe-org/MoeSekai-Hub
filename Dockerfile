FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
# the base image ships a "Caddy works!" index.html; the repo has none
RUN rm -f /usr/share/caddy/index.html
COPY . /usr/share/caddy
WORKDIR /usr/share/caddy
EXPOSE 8080
