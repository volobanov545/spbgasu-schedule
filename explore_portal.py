#!/usr/bin/env python3
"""Одноразовый скрипт для изучения структуры портала. Удалить после использования."""
import asyncio, os, re
from pathlib import Path
from playwright.async_api import async_playwright

PORTAL_URL   = "https://portal.spbgasu.ru"
PORTAL_LOGIN = os.environ["PORTAL_LOGIN"]
PORTAL_PASS  = os.environ["PORTAL_PASS"]
OUT          = Path(__file__).parent / "portal_explore"
OUT.mkdir(exist_ok=True)


async def save(page, name):
    (OUT / f"{name}.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    print(f"[SAVE] {name}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(60000)

        # Логин
        await page.goto(f"{PORTAL_URL}/auth/", wait_until="domcontentloaded")
        await page.wait_for_selector("input[name='USER_LOGIN']", state="visible")
        await page.locator("input[name='USER_LOGIN']").press_sequentially(PORTAL_LOGIN, delay=50)
        await page.locator("input[name='USER_PASSWORD']").press_sequentially(PORTAL_PASS, delay=50)
        async with page.expect_navigation(timeout=60000):
            await page.evaluate("document.querySelector('form').submit()")
        await page.wait_for_timeout(2000)
        print("[INFO] Залогинился")

        # Главная
        await page.goto(f"{PORTAL_URL}/lk/", wait_until="networkidle")
        await save(page, "01_main")

        # Профиль
        await page.goto(f"{PORTAL_URL}/lk/profile/", wait_until="networkidle")
        await save(page, "02_profile")

        # Расписание
        await page.goto(f"{PORTAL_URL}/lk/schedule/", wait_until="networkidle")
        await page.wait_for_timeout(3000)
        await save(page, "03_schedule")

        # Список журналов
        await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle")
        await page.wait_for_timeout(2000)
        await save(page, "04_journals_list")

        # Находим все ссылки на журналы кликом по строкам
        rows = page.locator("tbody tr")
        count = await rows.count()
        print(f"[INFO] Журналов: {count}")

        for i in range(count):
            row = rows.nth(i)
            cells = row.locator("td")
            if await cells.count() < 2:
                continue
            subject = (await cells.nth(1).inner_text()).strip()
            subject_slug = re.sub(r"[^\w]", "_", subject)[:40]

            try:
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                    await row.click()
                await page.wait_for_timeout(3000)  # ждём Vue рендер
                await save(page, f"05_journal_{i:02d}_{subject_slug}")
                print(f"[INFO]   URL: {page.url}")
            except Exception as e:
                print(f"[WARN] {subject}: {e}")
                await page.goto(f"{PORTAL_URL}/lk/journals/", wait_until="networkidle")
                await page.wait_for_timeout(1000)
                continue

            await page.go_back(wait_until="networkidle")
            await page.wait_for_timeout(1000)

        # Пробуем другие разделы
        for path, name in [
            ("/lk/group/",    "06_group"),
            ("/lk/faculty/",  "07_faculty"),
            ("/lk/news/",     "08_news"),
            ("/lk/search/",   "09_search"),
        ]:
            try:
                await page.goto(f"{PORTAL_URL}{path}", wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(2000)
                await save(page, name)
            except Exception as e:
                print(f"[WARN] {path}: {e}")

        await browser.close()
    print(f"\n[DONE] Файлы сохранены в {OUT}")


asyncio.run(main())
