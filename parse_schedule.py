#!/usr/bin/env python3
"""
Парсер расписания СПбГАСУ → .ics
Источник: rasp.spbgasu.ru (группа 3-СУЗСс-2)
"""

import re
import sys
import hashlib
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from icalendar.prop import vText

GROUP = "3-СУЗСс-2"
SCHEDULE_URL = "https://rasp.spbgasu.ru/"
OUTPUT_FILE = Path(__file__).parent / "schedule.ics"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://www.spbgasu.ru/",
}


def fetch_html_requests() -> str | None:
    """Лёгкая попытка без браузера. Возвращает None если данных группы нет."""
    try:
        resp = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        html = resp.text
        if GROUP not in html:
            print("[INFO] requests: данные группы отсутствуют (нужен JS-рендеринг)")
            return None
        print(f"[OK] requests: страница скачана ({len(html)} байт)")
        return html
    except Exception as e:
        print(f"[INFO] requests: {e}")
        return None


def fetch_html_playwright() -> str:
    """JS-рендеринг через Playwright."""
    from playwright.sync_api import sync_playwright

    print("[INFO] Playwright: запускаю браузер...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(extra_http_headers={"Referer": "https://www.spbgasu.ru/"})
        page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=90000)
        # Ждём появления элементов группы (JS инициализация)
        page.wait_for_selector(".get_data", timeout=30000)

        # Ищем и выбираем нашу группу
        group_el = page.locator(f".get_data[data-search='{GROUP}']")
        if group_el.count():
            group_el.first.click()
            page.wait_for_selector(".lesson", timeout=30000)
        else:
            # Попробуем через поле поиска
            search = page.locator("input[data-var='GROUPS']")
            if search.count():
                search.fill(GROUP)
                page.wait_for_timeout(1000)
                result = page.locator(f".get_data[data-search='{GROUP}']")
                if result.count():
                    result.first.click()
                    page.wait_for_selector(".lesson", timeout=30000)

        html = page.content()
        browser.close()

    if GROUP not in html:
        raise RuntimeError("Playwright: данные группы не найдены в странице")
    print(f"[OK] Playwright: страница получена ({len(html)} байт)")
    return html


def fetch_html(force_playwright: bool = False) -> str:
    if not force_playwright:
        html = fetch_html_requests()
        if html:
            return html
    return fetch_html_playwright()


def parse_time(text: str):
    m = re.search(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", text)
    if not m:
        return None, None
    start = datetime.strptime(m.group(1), "%H:%M").time()
    end = datetime.strptime(m.group(2), "%H:%M").time()
    return start, end


def parse_date(date_str: str) -> date | None:
    try:
        return datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def clean(text: str) -> str:
    return " ".join(text.split())


def parse_lessons(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    lessons = []

    for week_item in soup.select(".item"):
        time_div = week_item.select_one(".time")
        week_info = clean(time_div.get_text()) if time_div else ""

        for day_div in week_item.select(".days"):
            day_name_div = day_div.select_one(".week_day")
            if not day_name_div:
                continue
            date_div = day_name_div.select_one(".date")
            if not date_div:
                continue
            lesson_date = parse_date(date_div.get_text())
            if not lesson_date:
                continue

            for lesson in day_div.select(".lesson"):
                day_name_block = lesson.select_one(".day_name")
                if not day_name_block:
                    continue

                pair_tag = day_name_block.select_one("b")
                pair_text = clean(pair_tag.get_text()) if pair_tag else ""
                start_t, end_t = parse_time(day_name_block.get_text())
                if not start_t:
                    continue

                block = lesson.select_one(".lesson_block")
                if not block:
                    continue

                divs = [clean(d.get_text()) for d in block.find_all("div", recursive=False)]
                if len(divs) < 2:
                    continue

                subject = divs[0]
                room = teacher = ""
                if len(divs) == 4:
                    room, teacher = divs[2], divs[3]
                elif len(divs) == 3:
                    room, teacher = divs[1], divs[2]
                elif len(divs) == 2:
                    room = divs[1]

                lessons.append({
                    "date": lesson_date,
                    "start": start_t,
                    "end": end_t,
                    "pair": pair_text,
                    "subject": subject,
                    "room": room,
                    "teacher": teacher,
                    "week_info": week_info,
                })

    return lessons


def make_uid(lesson: dict) -> str:
    key = f"{lesson['date']}{lesson['start']}{lesson['subject']}{lesson['room']}"
    return hashlib.md5(key.encode()).hexdigest() + "@spbgasu"


def build_ics(lessons: list[dict], days_ahead: int | None = None) -> Calendar:
    today = date.today()
    cutoff = today + timedelta(days=days_ahead) if days_ahead else None

    filtered = [l for l in lessons if l["date"] >= today and (cutoff is None or l["date"] <= cutoff)]

    cal = Calendar()
    cal.add("prodid", "-//СПбГАСУ Schedule//3-СУЗСс-2//RU")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", f"СПбГАСУ {GROUP}")
    cal.add("x-wr-timezone", "Europe/Moscow")
    cal.add("x-wr-caldesc", f"Расписание занятий. Обновлено {today.strftime('%d.%m.%Y')}")

    for lesson in filtered:
        event = Event()
        event.add("summary", lesson["subject"])
        event.add("dtstart", datetime.combine(lesson["date"], lesson["start"]))
        event.add("dtend", datetime.combine(lesson["date"], lesson["end"]))
        event.add("location", vText(lesson["room"]))

        desc_parts = []
        if lesson["teacher"]:
            desc_parts.append(f"Преподаватель: {lesson['teacher']}")
        if lesson["pair"]:
            desc_parts.append(lesson["pair"])
        if lesson["week_info"]:
            desc_parts.append(lesson["week_info"])
        event.add("description", vText("\n".join(desc_parts)))
        event.add("uid", make_uid(lesson))
        cal.add_component(event)

    return cal, len(filtered)


def main():
    parser = argparse.ArgumentParser(description="Парсер расписания СПбГАСУ → .ics")
    parser.add_argument("--playwright", action="store_true", help="Принудительно использовать браузер")
    parser.add_argument("--file", type=Path, help="Читать из сохранённого HTML-файла")
    parser.add_argument("--days", type=int, default=None, help="Только N дней вперёд (по умолч. весь семестр)")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    args = parser.parse_args()

    if args.file:
        html = args.file.read_text(encoding="utf-8")
        print(f"[INFO] Читаю из файла: {args.file}")
    else:
        html = fetch_html(force_playwright=args.playwright)

    print("[INFO] Парсю расписание...")
    lessons = parse_lessons(html)

    if not lessons:
        print("[ERROR] Уроки не найдены.")
        sys.exit(1)

    cal, count = build_ics(lessons, days_ahead=args.days)
    args.output.write_bytes(cal.to_ical())

    period = f"ближайшие {args.days} дней" if args.days else f"{lessons[0]['date']} — {lessons[-1]['date']}"
    print(f"[OK] Событий в .ics: {count} ({period})")
    print(f"[OK] Сохранено: {args.output}")


if __name__ == "__main__":
    main()
