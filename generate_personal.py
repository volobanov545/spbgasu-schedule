#!/usr/bin/env python3
"""Генератор личных повторяющихся событий → .ics"""

from datetime import datetime, date, timedelta
from pathlib import Path
import hashlib

from icalendar import Calendar, Event, vRecur
from icalendar.prop import vText
import pytz

MSK = pytz.timezone("Europe/Moscow")
SCRIPT_DIR = Path(__file__).parent

# Следующий понедельник
today = date.today()
next_monday = today + timedelta(days=(7 - today.weekday()))


def make_uid(name: str, dt: datetime) -> str:
    return hashlib.md5(f"{name}{dt}".encode()).hexdigest() + "@personal"


def make_calendar(name: str, description: str) -> Calendar:
    cal = Calendar()
    cal.add("prodid", f"-//Personal//{name}//RU")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", "Europe/Moscow")
    cal.add("x-wr-caldesc", description)
    return cal


def add_weekly_event(cal: Calendar, title: str, start_dt: datetime, duration_hours: float, weekdays: list[int]):
    """Добавляет еженедельное повторяющееся событие для каждого дня из weekdays."""
    from datetime import timedelta as td
    for weekday in weekdays:
        # Найти ближайшую дату с нужным днём недели начиная с next_monday
        delta = (weekday - next_monday.weekday()) % 7
        event_date = next_monday + td(days=delta)
        dt_start = MSK.localize(datetime.combine(event_date, start_dt.time()))
        dt_end = dt_start + td(hours=duration_hours)

        event = Event()
        event.add("summary", title)
        event.add("dtstart", dt_start)
        event.add("dtend", dt_end)
        event.add("rrule", {"freq": "weekly", "byday": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][weekday][:2]})
        event.add("uid", make_uid(title + str(weekday), dt_start))
        cal.add_component(event)


# ─── Тренировки (Training) ───────────────────────────────────────────────────
training_cal = make_calendar("Training", "Тренировки: ПН и СР в 17:00")

for weekday in [0, 2]:  # 0=ПН, 2=СР
    delta = (weekday - next_monday.weekday()) % 7
    event_date = next_monday + timedelta(days=delta)
    dt_start = MSK.localize(datetime(event_date.year, event_date.month, event_date.day, 17, 0))
    dt_end = dt_start + timedelta(hours=1)

    day_abbr = ["MO", "WE"][0 if weekday == 0 else 1]
    event = Event()
    event.add("summary", "Training")
    event.add("dtstart", dt_start)
    event.add("dtend", dt_end)
    event.add("rrule", {"freq": "weekly", "byday": day_abbr})
    event.add("uid", make_uid("Training", dt_start))
    training_cal.add_component(event)

output = SCRIPT_DIR / "training.ics"
output.write_bytes(training_cal.to_ical())
print(f"[OK] {output}")

# ─── Доп. занятия (Extra Classes) ────────────────────────────────────────────
classes_cal = make_calendar("Extra Classes", "Доп. занятия: ЧТ в 14:00")

delta = (3 - next_monday.weekday()) % 7  # 3=ЧТ
event_date = next_monday + timedelta(days=delta)
dt_start = MSK.localize(datetime(event_date.year, event_date.month, event_date.day, 14, 0))
dt_end = dt_start + timedelta(hours=1)

event = Event()
event.add("summary", "Extra Classes (Strength of Materials)")
event.add("dtstart", dt_start)
event.add("dtend", dt_end)
event.add("rrule", {"freq": "weekly", "byday": "TH"})
event.add("uid", make_uid("ExtraClasses", dt_start))
classes_cal.add_component(event)

output = SCRIPT_DIR / "extra_classes.ics"
output.write_bytes(classes_cal.to_ical())
print(f"[OK] {output}")
