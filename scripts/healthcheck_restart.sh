#!/bin/bash
# Проверяет, отвечает ли бэкенд, и перезапускает systemd-сервис, если нет.
# Ловит именно "завис, но процесс жив" — то, что Restart=always не видит.

URL="http://127.0.0.1:8000/api/health"
LOG="/var/log/ocinka_healthcheck.log"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$URL")

if [ "$STATUS" != "200" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') UNHEALTHY (status=$STATUS) — restarting ocinka-backend" >> "$LOG"
    systemctl restart ocinka-backend
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') OK" >> "$LOG"
fi
