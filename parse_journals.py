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


async def login(page):
    page.set_default_timeout(60000)
    await page.goto(f"{PORTAL_URL}/auth/", wait_until="domcontentloaded", timeout=90000)

    login_sel = "input[name='USER_LOGIN'], input[name='login'], input[placeholder='Логин']"
    pass_sel  = "input[name='USER_PASSWORD'], input[name='password'], input[placeholder='Пароль']"

    await page.wait_for_selector(login_sel, state="visible", timeout=30000)
    await page.locator(login_sel).press_sequentially(PORTAL_LOGIN, delay=50)
    await page.locator(pass_sel).press_sequentially(PORTAL_PASS, delay=50)

    async with page.expect_navigation(timeout=60000):
        await page.evaluate("document.querySelector('form').submit()")
    await page.wait_for_timeout(2000)
    print("[INFO] Авторизация успешна")


async def get_journal_urls(page) -> list[tuple[str, str]]:
    """Кликает каждую строку в таблице журналов и собирает (предмет, url)."""
    await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle", timeout=60000)

    # Ждём появления строк таблицы
    try:
        await page.wait_for_selector("tbody tr", timeout=15000)
    except Exception:
        print("[WARN] Таблица журналов не появилась")
        return []

    rows_count = await page.locator("tbody tr").count()
    print(f"[INFO] Строк в таблице журналов: {rows_count}")

    results = []
    for i in range(rows_count):
        # Перечитываем строку каждый раз (DOM обновляется после навигации)
        row = page.locator("tbody tr").nth(i)

        # Берём название предмета из второй ячейки
        cells = row.locator("td")
        count = await cells.count()
        if count < 2:
            continue
        subject = (await cells.nth(1).inner_text()).strip()
        if not subject:
            continue

        # Кликаем строку и ждём навигации
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                await row.click()
            await page.wait_for_timeout(1500)
            url = page.url
            print(f"[INFO] Журнал: {subject} → {url}")
            results.append((subject, url))
        except Exception as e:
            print(f"[WARN] Не удалось открыть журнал '{subject}': {e}")
            await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle", timeout=30000)
            continue

        # Возвращаемся к списку
        await page.go_back(wait_until="networkidle", timeout=30000)
        try:
            await page.wait_for_selector("tbody tr", timeout=10000)
        except Exception:
            await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("tbody tr", timeout=10000)

    return results


def parse_journal_html(html: str, student_name: str, subject: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    # Ищем таблицу содержащую имя студента
    table = None
    for t in soup.find_all("table"):
        if t.find(string=re.compile(student_name, re.IGNORECASE)):
            table = t
            break

    if not table:
        print(f"[WARN] {subject}: таблица с '{student_name}' не найдена")
        return None

    rows = table.find_all("tr")
    if len(rows) < 2:
        return None

    # Заголовок — даты/названия колонок
    headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]

    # Строка студента
    student_cells = None
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if cells and re.search(student_name, cells[0].get_text(strip=True), re.IGNORECASE):
            student_cells = cells
            break

    if not student_cells:
        print(f"[WARN] {subject}: строка '{student_name}' не найдена")
        return None

    attendance   = {}
    attestations = {}

    for i, cell in enumerate(student_cells[1:], start=1):
        col_name = headers[i] if i < len(headers) else str(i)
        text     = cell.get_text(strip=True)
        classes  = " ".join(cell.get("class", []))

        if re.search(r"аттест", col_name, re.IGNORECASE) or "attest" in classes.lower():
            attestations[col_name] = text
        else:
            attendance[col_name] = text

    absences = [d for d, v in attendance.items() if v.lower() in ("н", "н/б", "-")]
    present  = [d for d, v in attendance.items() if v in ("✓", "+", "1", "б", "п")]

    print(f"[INFO]   присутствий: {len(present)}, пропусков: {len(absences)}, аттестаций: {len(attestations)}")

    return {
        "subject":       subject,
        "attendance":    attendance,
        "attestations":  attestations,
        "absences":      absences,
        "present_count": len(present),
        "absent_count":  len(absences),
    }


async def async_main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()

        await login(page)

        journal_urls = await get_journal_urls(page)
        if not journal_urls:
            print("[ERROR] Список журналов пуст")
            await browser.close()
            sys.exit(1)

        state = {}
        for subject, url in journal_urls:
            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
                # Ждём пока таблица с именем студента реально отрисуется
                try:
                    await page.wait_for_selector(f"text={STUDENT_NAME}", timeout=15000)
                except Exception:
                    print(f"[WARN] {subject}: имя '{STUDENT_NAME}' не появилось на странице")
                html   = await page.content()
                result = parse_journal_html(html, STUDENT_NAME, subject)
                if result:
                    state[subject] = result
            except Exception as e:
                print(f"[WARN] {subject}: ошибка при парсинге — {e}")

        await browser.close()

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] Сохранено {len(state)} журналов → {STATE_FILE}")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
