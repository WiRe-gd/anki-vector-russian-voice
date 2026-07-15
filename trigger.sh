#!/bin/bash
# trigger.sh — вызывается wire-pod'ом при срабатывании кастомного интента.
# $1 = распознанный текст (!speechText), $2 = серийный номер бота (!botSerial)
#
# ВАЖНО: скрипт должен завершаться мгновенно, иначе Vector "пожмёт плечами"
# по таймауту прошивки. Поэтому curl уходит в фон через nohup.
#
# Если демон крутится на другой машине — замените 127.0.0.1 на её IP.

DAEMON_URL="http://127.0.0.1:8090/ask"

nohup curl -s -m 180 -X POST "$DAEMON_URL" \
    --data-urlencode "text=$1" \
    --data-urlencode "serial=$2" \
    >/dev/null 2>&1 &

exit 0
