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
    current_date = None

    # Ищем все текстовые блоки страницы через обход тегов
    all_text_blocks = []
    for tag in soup.find_all(True):
        if tag.find(True):  # пропускаем контейнеры
            continue
        text = tag.get_text(strip=True)
        if text:
            all_text_blocks.append((tag, text))

    # Более надёжный подход: ищем структурные блоки
    # День — это заголовок вида "Пн 27.04.2026"
    day_pattern = re.compile(r"^(Пн|Вт|Ср|Чт|Пт|Сб|Вс)\s+\d{2}\.\d{2}\.\d{4}$")
    time_pattern = re.compile(r"\d{1,2}:\d{2}[–\-]\d{1,2}:\d{2}")

    # Ищем блоки дней через заголовочные теги (h1-h4) или div с нужным паттерном
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "div", "p", "span"]):
        text = tag.get_text(" ", strip=True)
        if day_pattern.match(text):
            current_date = parse_date(text)

        # Ищем карточки уроков — они содержат время и название предмета
        if current_date and time_pattern.search(text):
            event = extract_event(tag, current_date)
            if event:
                events.append(event)

    return events


def extract_event(tag, current_date) -> dict | None:
    """Извлекает данные урока из тега-карточки."""
    full_text = tag.get_text(" ", strip=True)

    time_m = re.search(r"(\d{1,2}:\d{2})[–\-](\d{1,2}:\d{2})", full_text)
    if not time_m:
        return None

    start_str = time_m.group(1)
    end_str   = time_m.group(2)

    # Тип занятия (лаб./пр./п. и т.д.)
    type_m = re.search(r"\b(лаб|пр|лек|п)\b\.?", full_text, re.IGNORECASE)
    lesson_type = type_m.group(0) if type_m else ""

    # Аудитория (формат: цифры/буква, например 102/С или 401/С)
    room_m = re.search(r"\b(\d{2,4}[/а-яА-Я\w]*)\b", full_text)
    room = room_m.group(1) if room_m else ""

    # Убираем время, тип, аудиторию — остаётся примерно название + преподаватель
    leftover = full_text
    leftover = re.sub(r"\d{1,2}:\d{2}[–\-]\d{1,2}:\d{2}", "", leftover)
    leftover = re.sub(r"\d+\s*пара", "", leftover)
    leftover = re.sub(r"\b(лаб|пр|лек|п)\b\.?", "", leftover, flags=re.IGNORECASE)
    leftover = re.sub(r"\b\d{2,4}[/\w]*\b", "", leftover)
    leftover = re.sub(r"\s+", " ", leftover).strip()

    # Преподаватель — формат "Фамилия И.О."
    teacher_m = re.findall(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.[А-ЯЁ]\.", leftover)
    teacher = ", ".join(teacher_m)

    # Группа
    group_m = re.search(r"\d+-[А-ЯЁа-яё]+-\d+", leftover)
    group = group_m.group(0) if group_m else ""

    # Название предмета — то что осталось после вычета учителя и группы
    subject = leftover
    for t in teacher_m:
        subject = subject.replace(t, "")
    if group:
        subject = subject.replace(group, "")
    subject = re.sub(r"\s+", " ", subject).strip().strip(",").strip()

    if not subject:
        return None

    def make_dt(time_str: str):
        h, m = map(int, time_str.split(":"))
        return datetime(current_date.year, current_date.month, current_date.day,
                        h, m, tzinfo=TZ)

    summary = f"{subject}"
    if lesson_type:
        summary += f" ({lesson_type})"

    return {
        "uid":      make_uid(str(current_date), start_str, subject),
        "summary":  summary,
        "dtstart":  make_dt(start_str),
        "dtend":    make_dt(end_str),
        "location": room,
        "description": "\n".join(filter(None, [teacher, group])),
    }


# ─── Playwright ──────────────────────────────────────────────────────────────

async def login(page):
    page.set_default_timeout(90000)
    await page.goto(f"{PORTAL_URL}/auth/", wait_until="domcontentloaded", timeout=90000)

    # Bitrix CMS: поля называются USER_LOGIN и USER_PASSWORD
    login_sel = "input[name='USER_LOGIN'], input[name='login'], input[placeholder='Логин']"
    pass_sel  = "input[name='USER_PASSWORD'], input[name='password'], input[placeholder='Пароль']"

    await page.wait_for_selector(login_sel, state="visible", timeout=30000)

    await page.click(login_sel)
    await page.press_sequentially(login_sel, PORTAL_LOGIN, delay=50)
    await page.click(pass_sel)
    await page.press_sequentially(pass_sel, PORTAL_PASS, delay=50)

    await page.screenshot(path=str(Path(__file__).parent / "debug_login.png"))
    print("[DEBUG] Форма заполнена, отправляю...")

    await page.locator("button[type='submit'], button:has-text('Войти')").click()
    await page.wait_for_load_state("domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)
    await page.screenshot(path=str(Path(__file__).parent / "debug_after_login.png"), full_page=True)
    print(f"[INFO] Авторизация: {page.url}")


async def go_to_schedule(page):
    await page.goto(f"{PORTAL_URL}/lk/schedule/", wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(3000)
    print(f"[INFO] Расписание: {page.url}")


async def click_arrow(page, direction: str):
    """Кликает < или > для навигации по неделям."""
    # Ищем SVG-стрелки или кнопки навигации
    selectors = (
        [f"button[aria-label*='редыдущ']", "button.prev", ".schedule-nav button:first-child",
         "svg[data-icon='chevron-left']", "button:has(svg):first-of-type"]
        if direction == "prev" else
        [f"button[aria-label*='ледующ']", "button.next", ".schedule-nav button:last-child",
         "svg[data-icon='chevron-right']", "button:has(svg):last-of-type"]
    )
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_load_state("networkidle")
                return
        except Exception:
            continue

    # Последний вариант — кликнуть по < или > в тексте
    arrow = "<" if direction == "prev" else ">"
    try:
        await page.get_by_text(arrow, exact=True).first.click()
        await page.wait_for_load_state("networkidle")
    except Exception as e:
        print(f"[WARN] Не удалось нажать {arrow}: {e}")


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

        # Проверяем что залогинились
        if "/auth" in page.url:
            print("[ERROR] Авторизация не прошла — проверь PORTAL_LOGIN/PORTAL_PASS")
            await browser.close()
            sys.exit(1)

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
