# Deploying Agora

Reference deployment: one Ubuntu 22.04 x86_64 VM (2 vCPU, 2 GB RAM + 2 GB swap), Cloudflare DNS (proxied),
nginx + Let's Encrypt on the host, everything else in Docker. Images come prebuilt from GitHub Actions.

## One-time setup

```bash
# Docker CE + compose plugin (official apt repo), log rotation, user in the docker group
echo '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' | sudo tee /etc/docker/daemon.json
# 2 GB swap so a memory spike never takes down other services on the box
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab; echo vm.swappiness=10 | sudo tee /etc/sysctl.d/99-agora-swap.conf

# images are private packages in the x-pai org: log in once with a classic token that has only read:packages
docker login ghcr.io -u <github-user>

# app directory: docker-compose.yml, .env, deploy/
sudo mkdir -p /srv/alphagora && sudo chown $USER /srv/alphagora
cp .env.example .env   # random POSTGRES_PASSWORD, ARENA_TOKEN_PEPPER (openssl rand -hex 32); chmod 600 .env
docker compose pull && docker compose up -d

# nginx + TLS (Cloudflare proxied record, SSL mode Full (strict))
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo cp deploy/nginx/agora.conf /etc/nginx/sites-available/agora   # keep only the port-80 server block
sudo ln -s /etc/nginx/sites-available/agora /etc/nginx/sites-enabled/ && sudo rm -f /etc/nginx/sites-enabled/default
./deploy/nginx/cloudflare-realip.sh
sudo certbot --nginx -d agora.alphabot.org.uk --non-interactive --agree-tos --register-unsafely-without-email --redirect
```

Postgres and Redis publish no host ports (the host may run its own). Only nginx is public; `web` listens on
127.0.0.1:3000 and serves `/api/v1` itself.

## Routine

| Task | Command (in /srv/alphagora) |
|---|---|
| Update to the latest images | `docker compose pull && docker compose up -d` |
| Pin a version | `AGORA_TAG=sha-xxxxxxx` in `.env`, then the line above |
| Logs | `docker compose logs -f arena-engine` |
| Live quotes | `ARENA_MARKET_FEED=redis` in `.env`, then `docker compose --profile live up -d` |
| OpenPup team | `sudo ./deploy/fetch-openpup.sh v0.2.2`; set `PUP_TEAM_TOKEN` + `OPENPUP_*` in `.env`; `docker compose --profile pup up -d` |
| Upgrade / roll back OpenPup | `sudo ./deploy/fetch-openpup.sh v0.2.3` / `--rollback` |

Restarting the engine keeps open orders (they are restored when their trade date reopens).
