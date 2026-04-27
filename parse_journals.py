#!/usr/bin/env python3
"""
Парсер журналов посещаемости и аттестаций СПбГАСУ.
Переменные окружения:
  PORTAL_LOGIN  — логин от портала
  PORTAL_PASS   — пароль от портала
  STUDENT_NAME  — часть фамилии для поиска (по умолчанию: Лобанов)
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

PORTAL_URL   = "https://portal.spbgasu.ru"
PORTAL_LOGIN = os.environ["PORTAL_LOGIN"]
PORTAL_PASS  = os.environ["PORTAL_PASS"]
STUDENT_NAME = os.environ.get("STUDENT_NAME", "Лобанов")
STATE_FILE   = Path(__file__).parent / "journals_state.json"


# ─── Playwright helpers ───────────────────────────────────────────────────────

async def login(page):
    page.set_default_timeout(90000)
    await page.goto(f"{PORTAL_URL}/auth/", wait_until="domcontentloaded", timeout=90000)

    login_sel = "input[name='USER_LOGIN'], input[name='login'], input[placeholder='Логин']"
    pass_sel  = "input[name='USER_PASSWORD'], input[name='password'], input[placeholder='Пароль']"

    await page.wait_for_selector(login_sel, state="visible", timeout=30000)
    await page.locator(login_sel).click()
    await page.locator(login_sel).press_sequentially(PORTAL_LOGIN, delay=50)
    await page.locator(pass_sel).click()
    await page.locator(pass_sel).press_sequentially(PORTAL_PASS, delay=50)

    async with page.expect_navigation(timeout=60000):
        await page.evaluate("document.querySelector('form').submit()")
    await page.wait_for_timeout(2000)

    page_text = await page.inner_text("body")
    if "USER_LOGIN" in await page.content() and "авторизовались" not in page_text:
        print("[ERROR] Авторизация не прошла")
        sys.exit(1)
    print("[INFO] Авторизация успешна")


async def get_journal_urls(page) -> list[tuple[str, str]]:
    """Возвращает список (предмет, url) из страницы /lk/journals/."""
    await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    results = []
    # Ищем все ссылки на журналы (/lk/journal-NNN/)
    for a in soup.find_all("a", href=re.compile(r"/lk/journal-\d+/")):
        url = a["href"]
        if not url.startswith("http"):
            url = PORTAL_URL + url
        # Название предмета — в строке таблицы
        tr = a.find_parent("tr")
        if tr:
            cells = tr.find_all("td")
            subject = cells[1].get_text(strip=True) if len(cells) > 1 else a.get_text(strip=True)
        else:
            subject = a.get_text(strip=True)
        results.append((subject, url))

    if not results:
        # Альтернатива: строки таблицы кликабельные, собираем через JS
        links = await page.eval_on_selector_all(
            "tr[onclick], tr[data-url]",
            "rows => rows.map(r => [r.getAttribute('onclick') || r.getAttribute('data-url'), r.querySelector('td:nth-child(2)')?.textContent?.trim() || ''])"
        )
        for onclick, subject in links:
            m = re.search(r"/lk/journal-(\d+)/", onclick or "")
            if m:
                results.append((subject, f"{PORTAL_URL}/lk/journal-{m.group(1)}/"))

    print(f"[INFO] Найдено журналов: {len(results)}")
    return results


# ─── Парсинг журнала ──────────────────────────────────────────────────────────

def parse_journal_html(html: str, student_name: str, subject: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    # Ищем таблицу журнала
    table = None
    for t in soup.find_all("table"):
        if t.find(string=re.compile(student_name, re.IGNORECASE)):
            table = t
            break

    if not table:
        print(f"[WARN] {subject}: таблица с '{student_name}' не найдена")
        return None

    # Заголовок — даты занятий
    header_row = table.find("tr")
    if not header_row:
        return None
    headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

    # Строка студента
    student_row = None
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if cells and re.search(student_name, cells[0].get_text(strip=True), re.IGNORECASE):
            student_row = cells
            break

    if not student_row:
        print(f"[WARN] {subject}: строка '{student_name}' не найдена в таблице")
        return None

    # Собираем посещаемость: {дата: метка}
    attendance = {}
    attestations = {}
    for i, cell in enumerate(student_row[1:], start=1):
        col_name = headers[i] if i < len(headers) else str(i)
        text = cell.get_text(strip=True)
        # Определяем тип колонки по заголовку или классу ячейки
        classes = " ".join(cell.get("class", []))
        if re.search(r"аттест", col_name, re.IGNORECASE) or "attest" in classes.lower():
            attestations[col_name] = text
        else:
            if text:  # пустая = не было занятия или не отмечено
                attendance[col_name] = text

    # Считаем пропуски (н, Н, или просто нет отметки о присутствии)
    absences = [d for d, v in attendance.items() if v.lower() in ("н", "н/б", "-")]
    present  = [d for d, v in attendance.items() if v in ("✓", "+", "1", "б")]

    print(f"[INFO] {subject}: присутствовал {len(present)}, пропусков {len(absences)}, аттестаций {len(attestations)}")

    return {
        "subject":      subject,
        "attendance":   attendance,
        "attestations": attestations,
        "absences":     absences,
        "present_count": len(present),
        "absent_count":  len(absences),
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

async def async_main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        await login(page)

        journal_urls = await get_journal_urls(page)
        if not journal_urls:
            print("[ERROR] Список журналов пуст — проверь страницу /lk/journals/")
            await browser.close()
            sys.exit(1)

        state = {}
        for subject, url in journal_urls:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2000)
                html = await page.content()
                result = parse_journal_html(html, STUDENT_NAME, subject)
                if result:
                    state[subject] = result
            except Exception as e:
                print(f"[WARN] {subject}: ошибка — {e}")

        await browser.close()

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] Сохранено {len(state)} журналов → {STATE_FILE}")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
