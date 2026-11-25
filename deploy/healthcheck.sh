#!/bin/sh
# Is the arena reachable? Checks the web app and, through it, the engine. Alerts after two consecutive
# failures (a restart is not an outage) and sends a recovery message. Cron: */2 * * * *
set -u
cd "$(dirname "$0")/.."
STATE="${HEALTH_STATE:-/tmp/agora-health.state}"
fails=$(cat "$STATE" 2>/dev/null || echo 0)
if curl -fsS --max-time 10 http://127.0.0.1:3000/api/v1/market/status > /dev/null 2>&1; then
  if [ "$fails" -ge 2 ]; then ./deploy/notify.sh ok "已恢复：擂台无法访问" "web / arena-engine 重新可以访问了。"; fi
  echo 0 > "$STATE"
else
  fails=$((fails + 1)); echo "$fails" > "$STATE"
  if [ "$fails" -eq 2 ]; then
    ./deploy/notify.sh critical "擂台无法访问" "连续两次（约 4 分钟）访问 web/api 失败。检查：cd /srv/alphagora && docker compose ps"
  fi
fi
