#!/usr/bin/env python3
"""
Сравнивает два ICS файла и отправляет уведомления об изменениях.
Использование: python notify.py old.ics new.ics
Переменные окружения:
  TG_TOKEN   — токен Telegram бота
  TG_CHANNEL — username или id канала (например @gasu4ka)
"""

import os
import sys
from datetime import datetime
from icalendar import Calendar


def load_events(ics_path: str) -> dict:
    """Загружает события из ICS, возвращает dict uid → dict."""
    try:
        data = open(ics_path, "rb").read()
    except FileNotFoundError:
        return {}
    cal = Calendar.from_ical(data)
    events = {}
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        uid = str(component.get("uid", ""))
        dt = component.get("dtstart")
        events[uid] = {
            "summary":     str(component.get("summary", "")),
            "dtstart":     dt.dt if dt else None,
            "dtend":       component.get("dtend").dt if component.get("dtend") else None,
            "location":    str(component.get("location", "")),
            "description": str(component.get("description", "")),
        }
    return events


def fmt_event(e: dict) -> str:
    dt = e["dtstart"]
    if dt:
        day = dt.strftime("%d.%m %a")
        time = dt.strftime("%H:%M")
        end  = e["dtend"].strftime("%H:%M") if e["dtend"] else ""
        time_str = f"{day} {time}–{end}"
    else:
        time_str = ""
    parts = [f"📚 {e['summary']}"]
    if time_str:
        parts.append(f"🕐 {time_str}")
    if e["location"]:
        parts.append(f"🚪 {e['location']}")
    if e["description"]:
        parts.append(f"👤 {e['description']}")
    return "\n".join(parts)


def build_diff_message(old: dict, new: dict) -> str | None:
    added   = {uid: e for uid, e in new.items() if uid not in old}
    removed = {uid: e for uid, e in old.items() if uid not in new}
    changed = {}
    for uid in new:
        if uid in old:
            o, n = old[uid], new[uid]
            if o["summary"] != n["summary"] or o["dtstart"] != n["dtstart"] \
               or o["location"] != n["location"] or o["description"] != n["description"]:
                changed[uid] = (o, n)

    if not added and not removed and not changed:
        return None

    lines = ["📅 Расписание обновилось!\n"]

    if added:
        lines.append(f"➕ Добавлено ({len(added)}):")
        for e in sorted(added.values(), key=lambda x: x["dtstart"] or datetime.min):
            lines.append(fmt_event(e))
            lines.append("")

    if removed:
        lines.append(f"➖ Убрано ({len(removed)}):")
        for e in sorted(removed.values(), key=lambda x: x["dtstart"] or datetime.min):
            lines.append(fmt_event(e))
            lines.append("")

    if changed:
        lines.append(f"✏️ Изменено ({len(changed)}):")
        for old_e, new_e in sorted(changed.values(), key=lambda x: x[1]["dtstart"] or datetime.min):
            lines.append(fmt_event(new_e))
            diffs = []
            if old_e["summary"] != new_e["summary"]:
                diffs.append(f"  было: {old_e['summary']}")
            if old_e["dtstart"] != new_e["dtstart"]:
                old_t = old_e["dtstart"].strftime("%d.%m %H:%M") if old_e["dtstart"] else "?"
                diffs.append(f"  время было: {old_t}")
            if old_e["location"] != new_e["location"]:
                diffs.append(f"  ауд. была: {old_e['location']}")
            if old_e["description"] != new_e["description"]:
                diffs.append(f"  препод. был: {old_e['description']}")
            lines.extend(diffs)
            lines.append("")

    return "\n".join(lines).strip()


# ─── Отправщики ──────────────────────────────────────────────────────────────

def send_telegram(text: str):
    token   = os.environ.get("TG_TOKEN", "")
    channel = os.environ.get("TG_CHANNEL", "")
    if not token or not channel:
        print("[NOTIFY] TG_TOKEN или TG_CHANNEL не заданы, пропускаю Telegram")
        return
    import urllib.request, urllib.parse, json
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": channel, "text": text}).encode()
    req  = urllib.request.Request(url, data=data)
    resp = urllib.request.urlopen(req, timeout=15)
    result = json.loads(resp.read())
    if result.get("ok"):
        print("[NOTIFY] Telegram: отправлено")
    else:
        print(f"[NOTIFY] Telegram: ошибка — {result}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Использование: notify.py old.ics new.ics")
        sys.exit(1)

    old = load_events(sys.argv[1])
    new = load_events(sys.argv[2])

    print(f"[NOTIFY] Старое расписание: {len(old)} событий")
    print(f"[NOTIFY] Новое расписание: {len(new)} событий")

    msg = build_diff_message(old, new)
    if not msg:
        print("[NOTIFY] Изменений нет, уведомления не нужны")
        return

    print(f"[NOTIFY] Найдены изменения, отправляю уведомления...")
    send_telegram(msg)


if __name__ == "__main__":
    main()
