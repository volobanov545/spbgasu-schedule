#!/bin/bash
cd /home/vla/schedule

python3 parse_schedule.py --playwright --days 14 --output schedule.ics >> update.log 2>&1

if git diff --quiet schedule.ics; then
    echo "[$(date '+%Y-%m-%d %H:%M')] Без изменений" >> update.log
else
    git add schedule.ics
    git commit -m "chore: update schedule $(date +'%Y-%m-%d')"
    git push >> update.log 2>&1
    echo "[$(date '+%Y-%m-%d %H:%M')] Обновлено и запушено" >> update.log
fi
