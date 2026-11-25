#!/bin/sh
# Sends one alert to the webhook configured in .env (ARENA_ALERT_WEBHOOK / ARENA_ALERT_FORMAT), the same one
# the engine uses. Usage: deploy/notify.sh <level: critical|warning|ok> <title> <text>
set -eu
cd "$(dirname "$0")/.."
get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- ; }
URL="$(get ARENA_ALERT_WEBHOOK)"; FMT="$(get ARENA_ALERT_FORMAT)"
echo "alert [$1] $2: $3" >&2
[ -n "$URL" ] || exit 0
BODY=$(python3 - "$1" "$2" "$3" "${FMT:-generic}" <<'PY'
import json, sys, datetime
level, title, text, fmt = sys.argv[1:5]
icon = {"critical": "🔴", "warning": "🟡"}.get(level, "🟢")
line = f"[Agora] {icon} {title}\n{text}"
body = {"wecom": {"msgtype": "text", "text": {"content": line}},
        "dingtalk": {"msgtype": "text", "text": {"content": line}},
        "feishu": {"msg_type": "text", "content": {"text": line}},
        "slack": {"text": line}}.get(fmt, {"source": "agora", "level": level, "title": title, "text": text,
                                           "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()})
print(json.dumps(body, ensure_ascii=False))
PY
)
RESP=$(curl -fsS --max-time 10 -H "Content-Type: application/json" -d "$BODY" "$URL") || { echo "notify: webhook failed" >&2; exit 1; }
# DingTalk / WeCom / Feishu return HTTP 200 with an error code in the body when they reject a message
echo "$RESP" | grep -Eq '"(errcode|code|StatusCode)":[[:space:]]*[1-9]' && { echo "notify: webhook rejected: $RESP" >&2; exit 1; }
exit 0
