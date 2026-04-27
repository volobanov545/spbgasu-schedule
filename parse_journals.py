#!/usr/bin/env python3
"""
Парсер журналов посещаемости и аттестаций СПбГАСУ.
Забирает аттестации и статистику с главной страницы ЛК,
затем ходит по журналам для точных данных о пропусках.
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
PORTAL_LOGIN = os.environ.get("PORTAL_LOGIN", "")
PORTAL_PASS  = os.environ.get("PORTAL_PASS", "")
STUDENT_NAME = os.environ.get("STUDENT_NAME", "Лобанов")
STATE_FILE   = Path(__file__).parent / "journals_state.json"
SESSION_DIR  = Path(os.environ.get("DATA_DIR", ".")) / "sessions"


# ─── Playwright ───────────────────────────────────────────────────────────────

def _session_file(portal_login: str) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{portal_login}.json"


async def login_with_session(context, portal_login: str, portal_pass: str):
    """Логинится используя сохранённые cookies если они живы, иначе логинится заново."""
    sf = _session_file(portal_login)
    if sf.exists():
        await context.add_cookies(json.loads(sf.read_text()))
        page = await context.new_page()
        await page.goto(f"{PORTAL_URL}/lk/", wait_until="domcontentloaded", timeout=30000)
        if "/auth/" not in page.url:
            print("[INFO] Сессия восстановлена из кэша")
            return page
        await page.close()
        print("[INFO] Сессия устарела, логинимся заново")

    page = await context.new_page()
    page.set_default_timeout(60000)
    await page.goto(f"{PORTAL_URL}/auth/", wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_selector("input[name='USER_LOGIN']", state="visible", timeout=30000)
    await page.locator("input[name='USER_LOGIN']").press_sequentially(portal_login, delay=50)
    await page.locator("input[name='USER_PASSWORD']").press_sequentially(portal_pass, delay=50)
    async with page.expect_navigation(timeout=60000):
        await page.evaluate("document.querySelector('form').submit()")
    await page.wait_for_timeout(2000)
    cookies = await context.cookies()
    sf.write_text(json.dumps(cookies))
    print("[INFO] Авторизация успешна, сессия сохранена")
    return page


async def login(page, login: str = "", password: str = ""):
    _login = login or PORTAL_LOGIN
    _pass  = password or PORTAL_PASS
    page.set_default_timeout(60000)
    await page.goto(f"{PORTAL_URL}/auth/", wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_selector("input[name='USER_LOGIN']", state="visible", timeout=30000)
    await page.locator("input[name='USER_LOGIN']").press_sequentially(_login, delay=50)
    await page.locator("input[name='USER_PASSWORD']").press_sequentially(_pass, delay=50)
    async with page.expect_navigation(timeout=60000):
        await page.evaluate("document.querySelector('form').submit()")
    await page.wait_for_timeout(2000)
    print("[INFO] Авторизация успешна")


# ─── Главная страница: аттестации и сводка посещаемости ──────────────────────

def parse_main_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # Сводка посещаемости — 4 числа в блоке grid-cols-12
    stats = {}
    for div in soup.find_all("div", class_=re.compile(r"grid-cols-12")):
        text = div.get_text(" ", strip=True)
        m = re.search(r"(\d+)\s*Проведено занятий", text)
        if m:
            stats["total_classes"] = int(m.group(1))
        for label, key in [
            ("Процент присутствий", "present_pct"),
            ("Процент отсутствий", "absent_pct"),
            ("Процент неотмеченных", "unmarked_pct"),
        ]:
            m = re.search(r"([\d.]+)%\s*" + label, text)
            if m:
                stats[key] = float(m.group(1))

    # Таблица аттестаций
    attestations = {}
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "1-я атт." not in headers and "2-я атт." not in " ".join(headers):
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            subject = cells[1].get_text(strip=True)
            att1    = cells[2].get_text(strip=True)
            att2    = cells[3].get_text(strip=True)
            if subject:
                attestations[subject] = {"att1": att1, "att2": att2}

    print(f"[INFO] Главная: {len(attestations)} предметов, посещаемость {stats.get('present_pct', '?')}%")
    return {"stats": stats, "attestations": attestations}


# ─── Индивидуальные журналы: пропуски ────────────────────────────────────────

def parse_journal_absences(html: str, student_name: str, subject: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="p-datatable-table")
    if not table:
        print(f"[WARN] {subject}: таблица не найдена")
        return None

    rows = table.find_all("tr")

    # Строка 1 — заголовки с датами
    header_cells = rows[1].find_all(["th", "td"]) if len(rows) > 1 else []
    headers = [c.get_text(strip=True) for c in header_cells]

    # Строка студента
    student_cells = None
    for row in rows[2:]:
        cells = row.find_all("td")
        if len(cells) > 1 and re.search(student_name, cells[1].get_text(strip=True), re.IGNORECASE):
            student_cells = cells
            break

    if not student_cells:
        print(f"[WARN] {subject}: строка '{student_name}' не найдена")
        return None

    absences = []
    present  = []

    for i, cell in enumerate(student_cells[2:], start=2):
        col = headers[i] if i < len(headers) else str(i)
        if "attestation-bg" in " ".join(cell.get("class", [])):
            continue  # аттестации берём с главной страницы

        div = cell.find("div", class_="attendance-content")
        if not div:
            continue
        div_cls = " ".join(div.get("class", []))

        if any(c in div_cls for c in ("attendance-by-prepod-present", "attendance-by-student")):
            present.append(col)
        elif any(c in div_cls for c in ("attendance-by-prepod-absent", "attendance-by-dekanat-sick")):
            absences.append(col)

    print(f"[INFO] {subject}: присутствий {len(present)}, пропусков {len(absences)}")
    return {"absences": absences, "present_count": len(present), "absent_count": len(absences)}


async def collect_journal_absences(page, student_name: str = "") -> dict:
    """Кликает по каждому журналу и парсит пропуски."""
    await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle", timeout=60000)
    try:
        await page.wait_for_selector("tbody tr", timeout=15000)
    except Exception:
        print("[WARN] Таблица журналов не появилась")
        return {}

    rows_count = await page.locator("tbody tr").count()
    print(f"[INFO] Журналов в таблице: {rows_count}")

    absences_by_subject = {}
    for i in range(rows_count):
        row   = page.locator("tbody tr").nth(i)
        cells = row.locator("td")
        if await cells.count() < 2:
            continue
        subject = (await cells.nth(1).inner_text()).strip()

        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                await row.click()
            # Ждём пока PrimeVue DataTable отрисует данные
            await page.wait_for_selector("table.p-datatable-table", timeout=15000)
            await page.wait_for_timeout(1500)
            html   = await page.content()
            result = parse_journal_absences(html, student_name or STUDENT_NAME, subject)
            if result:
                absences_by_subject[subject] = result
        except Exception as e:
            print(f"[WARN] {subject}: {e}")
            await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle", timeout=30000)
            continue

        await page.go_back(wait_until="networkidle", timeout=30000)
        try:
            await page.wait_for_selector("tbody tr", timeout=10000)
        except Exception:
            await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("tbody tr", timeout=10000)

    return absences_by_subject


# ─── Main ─────────────────────────────────────────────────────────────────────

async def async_main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()

        await login(page, PORTAL_LOGIN, PORTAL_PASS)

        # Главная страница — аттестации и сводка
        await page.goto(f"{PORTAL_URL}/lk/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)
        main_data = parse_main_page(await page.content())

        # Индивидуальные журналы — пропуски
        absences = await collect_journal_absences(page)

        await browser.close()

    # Объединяем в итоговое состояние
    state = {
        "stats":        main_data["stats"],
        "attestations": main_data["attestations"],
        "absences":     absences,
    }

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    subjects_ok = len(main_data["attestations"])
    absences_ok = len(absences)
    print(f"[DONE] Аттестации: {subjects_ok} предметов, пропуски: {absences_ok} журналов → {STATE_FILE}")


async def _async_run_for_user(portal_login: str, portal_pass: str, student_name: str) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()
        await login(page, portal_login, portal_pass)
        await page.goto(f"{PORTAL_URL}/lk/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)
        main_data = parse_main_page(await page.content())
        absences  = await collect_journal_absences(page, student_name)

        await browser.close()
    return {
        "stats":        main_data["stats"],
        "attestations": main_data["attestations"],
        "absences":     absences,
    }


def parse_lk_main(portal_login: str, portal_pass: str, student_name: str = "") -> dict:
    return asyncio.run(_async_run_for_user(portal_login, portal_pass, student_name or "Лобанов"))


async def _async_quick(portal_login: str, portal_pass: str) -> dict:
    """Только главная /lk/ — без обхода журналов. С кэшем сессии ~10 сек."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await login_with_session(context, portal_login, portal_pass)
        if "/lk/" not in page.url:
            await page.goto(f"{PORTAL_URL}/lk/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)
        main_data = parse_main_page(await page.content())
        await browser.close()
    return {
        "stats":        main_data["stats"],
        "attestations": main_data["attestations"],
        "absences":     {},
    }


def parse_lk_quick(portal_login: str, portal_pass: str) -> dict:
    return asyncio.run(_async_quick(portal_login, portal_pass))


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
