#!/usr/bin/env python3
"""
Сравнивает два ICS файла и отправляет уведомления об изменениях.
Использование: python notify.py old.ics new.ics
Переменные окружения:
  TG_TOKEN   — токен Telegram бота
  TG_CHANNEL — username или id канала (например @gasu4ka)
"""

import json
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

def _tg_send(chat_id: str, text: str, label: str):
    token = os.environ.get("TG_TOKEN", "")
    if not token or not chat_id:
        print(f"[NOTIFY] {label}: токен или chat_id не заданы, пропускаю")
        return
    import urllib.request, urllib.parse
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req  = urllib.request.Request(url, data=data)
    try:
        resp   = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        if result.get("ok"):
            print(f"[NOTIFY] {label}: отправлено")
        else:
            print(f"[NOTIFY] {label}: ошибка API — {result}")
    except Exception as e:
        print(f"[NOTIFY] {label}: сетевая ошибка — {e}")


def send_telegram(text: str):
    _tg_send(os.environ.get("TG_CHANNEL", ""), text, "Telegram канал")


def send_telegram_dm(text: str):
    _tg_send(os.environ.get("TG_OWNER_ID", ""), text, "Telegram DM")


# ─── Журналы ─────────────────────────────────────────────────────────────────

def load_journal_state(path: str) -> dict:
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def build_journal_diff_message(old: dict, new: dict) -> str | None:
    lines = []

    # Изменения в аттестациях (из главной страницы)
    old_att = old.get("attestations", {})
    new_att = new.get("attestations", {})
    for subject, new_marks in new_att.items():
        old_marks = old_att.get(subject, {})
        for key, label in [("att1", "1-я атт."), ("att2", "2-я атт.")]:
            n = new_marks.get(key, "—")
            o = old_marks.get(key, "—")
            if n != o and n not in ("—", ""):
                lines.append(f"📋 Аттестация — {subject}\n   {label}: {o} → {n}")

    # Изменения в пропусках (из индивидуальных журналов)
    old_abs = old.get("absences", {})
    new_abs = new.get("absences", {})
    for subject, new_data in new_abs.items():
        old_data = old_abs.get(subject, {})
        added = set(new_data.get("absences", [])) - set(old_data.get("absences", []))
        if added:
            dates = ", ".join(sorted(added))
            lines.append(f"❌ Новый пропуск — {subject}\n   {dates}")

    # Изменение % отсутствий
    old_pct = old.get("stats", {}).get("absent_pct")
    new_pct = new.get("stats", {}).get("absent_pct")
    if old_pct is not None and new_pct is not None and new_pct > old_pct + 1:
        lines.append(f"📊 Процент отсутствий вырос: {old_pct}% → {new_pct}%")

    if not lines:
        return None

    return "🎓 Обновления по журналам:\n\n" + "\n\n".join(lines)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Использование: notify.py old.ics new.ics [old_journals.json new_journals.json]")
        sys.exit(1)

    # Расписание
    old = load_events(sys.argv[1])
    new = load_events(sys.argv[2])

    print(f"[NOTIFY] Старое расписание: {len(old)} событий")
    print(f"[NOTIFY] Новое расписание: {len(new)} событий")

    msg = build_diff_message(old, new)
    if not msg:
        print("[NOTIFY] Расписание не изменилось")
    else:
        print("[NOTIFY] Найдены изменения в расписании, отправляю в канал...")
        send_telegram(msg)

    # Журналы (опционально)
    if len(sys.argv) >= 5:
        old_j = load_journal_state(sys.argv[3])
        new_j = load_journal_state(sys.argv[4])
        if not old_j.get("attestations"):
            print("[NOTIFY] Журналы: первый запуск, сохраняю baseline без уведомлений")
        else:
            jmsg = build_journal_diff_message(old_j, new_j)
            if not jmsg:
                print("[NOTIFY] Журналы не изменились")
            else:
                print("[NOTIFY] Найдены изменения в журналах, отправляю в личку...")
                send_telegram_dm(jmsg)


if __name__ == "__main__":
    main()
