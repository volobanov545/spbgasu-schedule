#!/usr/bin/env python3
"""
Синхронизирует schedule.ics → Яндекс.Календарь через CalDAV.
Переменные окружения:
  YANDEX_LOGIN    — логин (без @yandex.ru)
  YANDEX_APPPASS  — пароль приложения из id.yandex.ru
  YANDEX_CAL_NAME — название календаря (по умолчанию: СПбГАСУ)
"""

import os
import sys
from pathlib import Path

import caldav
from icalendar import Calendar

ICS_FILE = Path(__file__).parent / "schedule.ics"

CALDAV_URL  = "https://caldav.yandex.ru"
LOGIN       = os.environ["YANDEX_LOGIN"]
APPPASS     = os.environ["YANDEX_APPPASS"]
CAL_NAME    = os.environ.get("YANDEX_CAL_NAME", "СПбГАСУ")


def main():
    if not ICS_FILE.exists():
        print(f"[ERROR] Файл не найден: {ICS_FILE}")
        sys.exit(1)

    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=f"{LOGIN}@yandex.ru",
        password=APPPASS,
    )

    principal = client.principal()
    calendars = principal.calendars()

    calendar = next((c for c in calendars if c.name == CAL_NAME), None)
    if calendar is None:
        calendar = principal.make_calendar(name=CAL_NAME)
        print(f"[INFO] Создан новый календарь: {CAL_NAME}")
    else:
        print(f"[INFO] Найден календарь: {CAL_NAME}")

    # Читаем новые события из ICS
    with open(ICS_FILE, "rb") as f:
        raw = f.read()

    cal = Calendar.from_ical(raw)
    new_events: dict[str, bytes] = {}
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        uid = str(component.get("UID"))
        single = Calendar()
        single.add("prodid", "-//SPbGASU Schedule//RU")
        single.add("version", "2.0")
        single.add_component(component)
        new_events[uid] = single.to_ical()

    # Удаляем события которых больше нет
    deleted = 0
    for event in calendar.events():
        try:
            ec = Calendar.from_ical(event.data)
            for comp in ec.walk():
                if comp.name == "VEVENT":
                    uid = str(comp.get("UID"))
                    if uid not in new_events:
                        event.delete()
                        deleted += 1
        except Exception as e:
            print(f"[WARN] {e}")

    # Добавляем / обновляем события
    synced = 0
    for uid, ical_bytes in new_events.items():
        try:
            calendar.save_event(ical_bytes.decode())
            synced += 1
        except Exception as e:
            print(f"[WARN] {uid}: {e}")

    print(f"[DONE] Синхронизировано: {synced}, удалено: {deleted}")


if __name__ == "__main__":
    main()
