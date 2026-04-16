#!/bin/bash
# Обновление расписания СПбГАСУ
# Запускается через cron. Пробует скачать живую страницу,
# при неудаче (сайт недоступен/требует JS) оставляет старый .ics.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/update.log"

echo "[$(date '+%Y-%m-%d %H:%M')] Обновление расписания..." >> "$LOG"

python3 "$SCRIPT_DIR/parse_schedule.py" >> "$LOG" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M')] OK" >> "$LOG"
else
    echo "[$(date '+%Y-%m-%d %H:%M')] ОШИБКА (код $STATUS)" >> "$LOG"
fi
