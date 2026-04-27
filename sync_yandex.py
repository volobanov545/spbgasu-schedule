#!/usr/bin/env python3
"""
Синхронизирует schedule.ics → Яндекс.Календарь через CalDAV.
Переменные окружения:
  YANDEX_LOGIN    — логин (без @yandex.ru)
  YANDEX_APPPASS  — пароль приложения из id.yandex.ru
  YANDEX_CAL_NAME — название календаря (по умолчанию: СПбГАСУ)
"""

import io
import os
import sys
import urllib.request
from pathlib import Path

import caldav
from icalendar import Calendar

ICS_FILE    = Path(__file__).parent / "schedule.ics"
ICS_URL     = "https://gitverse.ru/api/repos/volobanov5/spbgasu-schedule/raw/branch/main/schedule.ics"


def _fetch_ics(ics_path: Path | None) -> bytes:
    if ics_path and ics_path.exists():
        return ics_path.read_bytes()
    with urllib.request.urlopen(ICS_URL, timeout=30) as r:
        return r.read()

CALDAV_URL  = "https://caldav.yandex.ru"
LOGIN       = os.environ.get("YANDEX_LOGIN", "")
APPPASS     = os.environ.get("YANDEX_APPPASS", "")
CAL_NAME    = os.environ.get("YANDEX_CAL_NAME", "СПбГАСУ")


def sync_calendar(ylogin: str, ypass: str, ics_path: Path | None = None, cal_name: str = "СПбГАСУ"):
    """Синхронизирует ICS в Яндекс.Календарь. Вызывается ботом для каждого пользователя."""
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=f"{ylogin}@yandex.ru",
        password=ypass,
    )
    principal = client.principal()
    calendars = principal.calendars()

    is_new = False
    calendar = next((c for c in calendars if c.get_display_name() == cal_name), None)
    if calendar is None:
        calendar = principal.make_calendar(name=cal_name)
        is_new = True

    raw = _fetch_ics(ics_path)
    cal = Calendar.from_ical(raw)
    new_events: dict[str, bytes] = {}
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        uid = str(component.get("UID"))
        single = Calendar()
        single.add("prodid", "-//SPbГАСУ Schedule//RU")
        single.add("version", "2.0")
        single.add_component(component)
        new_events[uid] = single.to_ical()

    if not is_new:
        for event in calendar.events():
            try:
                ec = Calendar.from_ical(event.data)
                for comp in ec.walk():
                    if comp.name == "VEVENT":
                        if str(comp.get("UID")) not in new_events:
                            event.delete()
            except Exception:
                pass

    synced = 0
    first_err = None
    for ical_bytes in new_events.values():
        try:
            calendar.save_event(ical_bytes.decode())
            synced += 1
        except Exception as e:
            if first_err is None:
                first_err = e

    if synced == 0 and new_events:
        raise Exception(f"Не удалось сохранить ни одного события ({len(new_events)} шт.): {first_err}")

    return synced


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

    is_new = False
    calendar = next((c for c in calendars if c.get_display_name() == CAL_NAME), None)
    if calendar is None:
        calendar = principal.make_calendar(name=CAL_NAME)
        is_new = True
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
        single.add("prodid", "-//SPbГАСУ Schedule//RU")
        single.add("version", "2.0")
        single.add_component(component)
        new_events[uid] = single.to_ical()

    # Удаляем события которых больше нет (пропускаем для нового календаря)
    deleted = 0
    if not is_new:
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
