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
import threading
from pathlib import Path

# Не более 3 одновременных браузеров — иначе Amvera контейнер падает по памяти
_browser_sem = threading.Semaphore(3)

import logging

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

PORTAL_URL   = "https://portal.spbgasu.ru"
PORTAL_LOGIN = os.environ.get("PORTAL_LOGIN", "")
PORTAL_PASS  = os.environ.get("PORTAL_PASS", "")
STUDENT_NAME = os.environ.get("STUDENT_NAME", "Лобанов")
STATE_FILE   = Path(__file__).parent / "journals_state.json"
SESSION_DIR  = Path(os.environ.get("DATA_DIR", ".")) / "sessions"
DATA_DIR     = Path(os.environ.get("DATA_DIR", "."))


def _save_debug_html(html: str, filename: str):
    try:
        (DATA_DIR / filename).write_text(html, encoding="utf-8")
        log.info("Debug HTML сохранён: %s (%d байт)", filename, len(html))
    except Exception as e:
        log.warning("Не удалось сохранить debug HTML: %s", e)


# ─── Playwright ───────────────────────────────────────────────────────────────

def _safe_login(login: str) -> str:
    """Только безопасные символы для имени файла — убирает path traversal."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', login) or "unknown"


def _session_file(portal_login: str) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{_safe_login(portal_login)}.json"


async def login_with_session(context, portal_login: str, portal_pass: str):
    """Логинится используя сохранённые cookies если они живы, иначе логинится заново."""
    sf = _session_file(portal_login)
    if sf.exists():
        await context.add_cookies(json.loads(sf.read_text()))
        page = await context.new_page()
        await page.goto(f"{PORTAL_URL}/lk/", wait_until="networkidle", timeout=60000)
        try:
            # Ждём форму логина до 7 сек: если появилась — сессия протухла
            await page.wait_for_selector("input[name='USER_LOGIN']", timeout=7000)
            await page.close()
            sf.unlink(missing_ok=True)
            log.info("Сессия устарела (форма логина появилась), логинимся заново")
        except Exception:
            # Форма не появилась — значит сессия жива
            log.info("Сессия восстановлена из кэша")
            return page

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
    log.info("Авторизация успешна, сессия сохранена")
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
    log.info("Авторизация успешна")


# ─── Главная страница: аттестации и сводка посещаемости ──────────────────────

def parse_student_name(html: str) -> str:
    """
    Извлекает фамилию студента из HTML страницы /lk/.
    Портал Bitrix обычно показывает ФИО в профиле или приветственном блоке.
    Возвращает первое слово (фамилию) или пустую строку если не нашли.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Ищем элементы, которые Bitrix использует для отображения имени пользователя
    candidates = []
    for el in soup.find_all(True):
        cls = " ".join(el.get("class", []))
        if re.search(r"user[-_]?name|personal[-_]?name|fio|profile[-_]?name|greeting", cls, re.I):
            candidates.append(el.get_text(strip=True))
    # Если не нашли по классу — ищем в тексте приветствия ("Здравствуйте, Иванов")
    if not candidates:
        text = soup.get_text(" ", strip=True)
        m = re.search(r"(?:Здравствуйте|Добро пожаловать)[,!]?\s+([А-ЯЁ][а-яё]+)", text)
        if m:
            candidates.append(m.group(1))
    for c in candidates:
        words = c.split()
        # Берём первое кириллическое слово длиннее 2 символов — скорее всего фамилия
        for w in words:
            if re.match(r'^[А-ЯЁа-яё]{3,}$', w):
                return w.capitalize()
    return ""


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
    all_tables = soup.find_all("table")
    log.info("Таблиц на странице: %d", len(all_tables))
    for table in all_tables:
        ths = table.find_all("th")
        if ths:
            headers = [th.get_text(strip=True) for th in ths]
        else:
            first_row = table.find("tr")
            headers = [td.get_text(strip=True) for td in first_row.find_all("td")] if first_row else []
        log.info("Заголовки таблицы: %s", headers[:8])

        att1_col = att2_col = subj_col = None
        for i, h in enumerate(headers):
            hl = h.lower()
            if "1" in hl and "атт" in hl:
                att1_col = i
            elif "2" in hl and "атт" in hl:
                att2_col = i
            elif any(w in hl for w in ("дисциплин", "предмет", "наименован")):
                subj_col = i

        if att1_col is None and att2_col is None:
            continue
        if subj_col is None:
            subj_col = 1

        max_col = max(c for c in [att1_col, att2_col, subj_col] if c is not None)
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) <= max_col:
                continue
            subject = cells[subj_col].get_text(strip=True)
            att1 = cells[att1_col].get_text(strip=True) if att1_col is not None else ""
            att2 = cells[att2_col].get_text(strip=True) if att2_col is not None else ""
            if subject:
                attestations[subject] = {
                    "att1": att1 or "—",
                    "att2": att2 or "—",
                }

    log.info("Главная: %d предметов, посещаемость %s%%", len(attestations), stats.get("present_pct", "?"))
    return {"stats": stats, "attestations": attestations}


# ─── Индивидуальные журналы: пропуски ────────────────────────────────────────

def parse_journal_absences(html: str, student_name: str, subject: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="p-datatable-table")
    if not table:
        log.warning("%s: таблица не найдена", subject)
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
        log.warning("%s: строка '%s' не найдена", subject, student_name)
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

    log.info("%s: присутствий %d, пропусков %d", subject, len(present), len(absences))
    return {"absences": absences, "present_count": len(present), "absent_count": len(absences)}


async def collect_journal_absences(page, student_name: str = "") -> dict:
    """Кликает по каждому журналу и парсит пропуски."""
    await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle", timeout=60000)
    try:
        await page.wait_for_selector("tbody tr", timeout=15000)
    except Exception:
        log.warning("Таблица журналов не появилась")
        return {}

    rows_count = await page.locator("tbody tr").count()
    log.info("Журналов в таблице: %d", rows_count)

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
            log.warning("%s: %s", subject, e)
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
    log.info("Аттестации: %d предметов, пропуски: %d журналов → %s", subjects_ok, absences_ok, STATE_FILE)


async def _async_run_for_user(portal_login: str, portal_pass: str, student_name: str) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()
        await login(page, portal_login, portal_pass)
        await page.goto(f"{PORTAL_URL}/lk/", wait_until="networkidle", timeout=60000)
        try:
            await page.wait_for_selector("table", timeout=8000)
        except Exception:
            log.warning("Таблица на /lk/ не появилась за 8 сек")
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('table tr').length > 2",
                timeout=12000,
            )
        except Exception:
            log.warning("Таблица не заполнилась за 12 сек — берём что есть")
        await page.wait_for_timeout(500)
        html = await page.content()
        _save_debug_html(html, f"debug_lk_{_safe_login(portal_login)}.html")
        main_data = parse_main_page(html)
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
    """Только главная /lk/ — без обхода журналов. Свежий логин каждый раз."""
    _browser_sem.acquire()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page    = await browser.new_page()
            await login(page, portal_login, portal_pass)
            await page.goto(f"{PORTAL_URL}/lk/", wait_until="networkidle", timeout=60000)
            try:
                await page.wait_for_selector("table", timeout=8000)
            except Exception:
                log.warning("Таблица на /lk/ не появилась за 8 сек")
            # Ждём пока в таблице появятся реальные строки с данными
            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('table tr').length > 2",
                    timeout=12000,
                )
                log.info("Таблица заполнилась данными")
            except Exception:
                log.warning("Таблица не заполнилась за 12 сек — берём что есть")
            await page.wait_for_timeout(500)
            html = await page.content()
            _save_debug_html(html, f"debug_lk_{_safe_login(portal_login)}.html")
            await page.screenshot(path=str(DATA_DIR / f"debug_lk_{_safe_login(portal_login)}.png"), full_page=True)
            log.info("Скриншот сохранён: debug_lk_%s.png", portal_login)
            main_data    = parse_main_page(html)
            student_name = parse_student_name(html)
            await browser.close()
    finally:
        _browser_sem.release()
    return {
        "stats":        main_data["stats"],
        "attestations": main_data["attestations"],
        "absences":     {},
        "student_name": student_name,
    }


def parse_lk_quick(portal_login: str, portal_pass: str) -> dict:
    return asyncio.run(_async_quick(portal_login, portal_pass))


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
