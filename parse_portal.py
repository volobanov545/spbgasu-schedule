#!/usr/bin/env python3
"""
Парсер расписания СПбГАСУ через личный кабинет portal.spbgasu.ru
Переменные окружения:
  PORTAL_LOGIN — логин от портала
  PORTAL_PASS  — пароль от портала
"""

import asyncio
import hashlib
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

PORTAL_URL   = "https://portal.spbgasu.ru"
PORTAL_LOGIN = os.environ["PORTAL_LOGIN"]
PORTAL_PASS  = os.environ["PORTAL_PASS"]
OUTPUT_FILE  = Path(__file__).parent / "schedule.ics"
TZ           = ZoneInfo("Europe/Moscow")
WEEKS        = 4  # прошлая + текущая + 2 следующие


# ─── Парсинг HTML ────────────────────────────────────────────────────────────

def parse_date(text: str):
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
    return None


def parse_time_range(text: str):
    m = re.search(r"(\d{1,2}:\d{2})[–\-](\d{1,2}:\d{2})", text)
    if m:
        return m.group(1), m.group(2)
    return None, None


def make_uid(day_str: str, start: str, subject: str) -> str:
    raw = f"portal-spbgasu-{day_str}-{start}-{subject}"
    return hashlib.md5(raw.encode()).hexdigest() + "@portal.spbgasu.ru"


def parse_schedule_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    events = []

    # Заголовки дней — h2 с датой "Пн 20.04.2026"
    day_pattern = re.compile(r"(Пн|Вт|Ср|Чт|Пт|Сб|Вс)\s+\d{2}\.\d{2}\.\d{4}")

    for h2 in soup.find_all("h2"):
        h2_text = h2.get_text(strip=True)
        if not day_pattern.search(h2_text):
            continue
        current_date = parse_date(h2_text)
        if not current_date:
            continue

        # Карточки событий идут после h2 в div.schedule-content
        content = h2.find_next_sibling("div", class_="schedule-content")
        if not content:
            continue

        for card in content.find_all("div", attrs={"data-pd-tooltip": "true"}):
            event = extract_event(card, current_date)
            if event:
                events.append(event)

    return events


def extract_event(card, current_date) -> dict | None:
    """Извлекает данные урока из карточки div[data-pd-tooltip]."""

    # Время: span.text-l.font-bold → "9:00-10:30"
    time_tag = card.find("span", class_="font-bold")
    if not time_tag:
        return None
    time_text = time_tag.get_text(strip=True)
    m = re.match(r"(\d{1,2}:\d{2})[–\-](\d{1,2}:\d{2})", time_text)
    if not m:
        return None
    start_str, end_str = m.group(1), m.group(2)

    # Предмет: h3.text-lg.text-gray-600
    h3 = card.find("h3", class_="text-lg")
    if not h3:
        return None
    # Тип занятия — span внутри h3
    type_span = h3.find("span")
    lesson_type = type_span.get_text(strip=True) if type_span else ""
    if type_span:
        type_span.decompose()
    subject = h3.get_text(strip=True)

    # Аудитория и преподаватели — col-span-2 блоки
    cols = card.find_all("div", class_="col-span-2")
    room = ""
    teacher = ""
    for col in cols:
        label = col.find("span", class_="text-gray-500")
        if not label:
            continue
        label_text = label.get_text(strip=True)
        value_span = col.find("span", class_="text-gray-700")
        value = value_span.get_text(strip=True) if value_span else ""
        if "Аудитория" in label_text:
            room = value
        elif "Преподаватели" in label_text:
            teacher = value

    if not subject:
        return None

    def make_dt(t: str):
        h, mn = map(int, t.split(":"))
        return datetime(current_date.year, current_date.month, current_date.day,
                        h, mn, tzinfo=TZ)

    summary = subject
    if lesson_type:
        summary += f" ({lesson_type})"

    return {
        "uid":         make_uid(str(current_date), start_str, subject),
        "summary":     summary,
        "dtstart":     make_dt(start_str),
        "dtend":       make_dt(end_str),
        "location":    room,
        "description": teacher,
    }


# ─── Playwright ──────────────────────────────────────────────────────────────

async def login(page):
    page.set_default_timeout(90000)
    await page.goto(f"{PORTAL_URL}/auth/", wait_until="domcontentloaded", timeout=90000)

    # Bitrix CMS: поля называются USER_LOGIN и USER_PASSWORD
    login_sel = "input[name='USER_LOGIN'], input[name='login'], input[placeholder='Логин']"
    pass_sel  = "input[name='USER_PASSWORD'], input[name='password'], input[placeholder='Пароль']"

    await page.wait_for_selector(login_sel, state="visible", timeout=30000)

    await page.locator(login_sel).click()
    await page.locator(login_sel).press_sequentially(PORTAL_LOGIN, delay=50)
    await page.locator(pass_sel).click()
    await page.locator(pass_sel).press_sequentially(PORTAL_PASS, delay=50)

    await page.screenshot(path=str(Path(__file__).parent / "debug_login.png"))
    print("[DEBUG] Форма заполнена, отправляю...")

    # Ждём навигации явно
    async with page.expect_navigation(timeout=60000):
        await page.evaluate("document.querySelector('form').submit()")

    await page.wait_for_timeout(2000)
    await page.screenshot(path=str(Path(__file__).parent / "debug_after_login.png"), full_page=True)
    print(f"[INFO] Авторизация: {page.url}")


async def go_to_schedule(page):
    await page.goto(f"{PORTAL_URL}/lk/schedule/", wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(3000)
    print(f"[INFO] Расписание: {page.url}")


async def click_arrow(page, direction: str):
    """Кликает кнопку навигации по неделям (PrimeVue pi-chevron)."""
    icon_class = "pi-chevron-left" if direction == "prev" else "pi-chevron-right"
    sel = f"button:has(.{icon_class})"

    # Запоминаем текущую неделю чтобы дождаться смены
    old_week = await page.locator("span.text-lg.font-semibold").first.inner_text()

    btn = page.locator(sel).first
    await btn.click(timeout=10000)

    # Ждём пока текст недели изменится (SPA обновляет без перезагрузки)
    for _ in range(20):
        await page.wait_for_timeout(500)
        new_week = await page.locator("span.text-lg.font-semibold").first.inner_text()
        if new_week != old_week:
            print(f"[INFO] Переход: {old_week} → {new_week}")
            return

    print(f"[WARN] Неделя не сменилась после клика {direction}")


async def collect_events(page) -> list[dict]:
    """Собирает события за WEEKS недель."""
    all_events: dict[str, dict] = {}

    # Идём на 1 неделю назад
    await click_arrow(page, "prev")

    for week_i in range(WEEKS):
        html = await page.content()
        events = parse_schedule_html(html)
        print(f"[INFO] Неделя {week_i + 1}: найдено {len(events)} занятий")
        for e in events:
            all_events[e["uid"]] = e  # дедупликация по UID

        if week_i < WEEKS - 1:
            await click_arrow(page, "next")

    return list(all_events.values())


# ─── ICS ─────────────────────────────────────────────────────────────────────

def build_ics(events: list[dict]) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//SPbGASU Portal Schedule//RU")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "СПбГАСУ Расписание")
    cal.add("x-wr-timezone", "Europe/Moscow")

    for e in events:
        ev = Event()
        ev.add("uid",         e["uid"])
        ev.add("summary",     e["summary"])
        ev.add("dtstart",     e["dtstart"])
        ev.add("dtend",       e["dtend"])
        ev.add("location",    e.get("location", ""))
        ev.add("description", e.get("description", ""))
        cal.add_component(ev)

    return cal.to_ical()


# ─── Main ────────────────────────────────────────────────────────────────────

async def async_main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        await login(page)

        # Bitrix после логина остаётся на /auth/ — проверяем по содержимому
        page_text = await page.inner_text("body")
        if "USER_LOGIN" in await page.content() and "авторизовались" not in page_text:
            print("[ERROR] Авторизация не прошла — проверь PORTAL_LOGIN/PORTAL_PASS")
            await browser.close()
            sys.exit(1)
        print("[INFO] Авторизация успешна")

        await go_to_schedule(page)

        # Сохраняем HTML и скриншот для отладки
        debug_html = Path(__file__).parent / "debug_schedule.html"
        debug_png  = Path(__file__).parent / "debug_schedule.png"
        debug_html.write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(debug_png), full_page=True)
        print(f"[DEBUG] HTML сохранён: {debug_html}")
        print(f"[DEBUG] Скриншот сохранён: {debug_png}")

        events = await collect_events(page)
        await browser.close()

    if not events:
        print("[WARN] Не найдено ни одного занятия — проверь debug_schedule.html")
        sys.exit(1)

    ics_data = build_ics(events)
    OUTPUT_FILE.write_bytes(ics_data)
    print(f"[DONE] Сохранено {len(events)} событий → {OUTPUT_FILE}")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
