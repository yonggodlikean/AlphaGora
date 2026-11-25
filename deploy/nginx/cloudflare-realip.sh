#!/bin/sh
# Writes /etc/nginx/conf.d/cloudflare-realip.conf so nginx takes the visitor IP from CF-Connecting-IP, but only
# when the request really comes from a Cloudflare edge. Re-run occasionally (Cloudflare publishes its ranges).
set -eu
{ echo "# Cloudflare edge ranges (https://www.cloudflare.com/ips/): trust CF-Connecting-IP only from them."
  for u in https://www.cloudflare.com/ips-v4 https://www.cloudflare.com/ips-v6; do
    curl -fsS --max-time 20 "$u" | sed 's/^/set_real_ip_from /; s/$/;/'
  done
  echo "real_ip_header CF-Connecting-IP;"; } | sudo tee /etc/nginx/conf.d/cloudflare-realip.conf > /dev/null
sudo nginx -t && sudo systemctl reload nginx
