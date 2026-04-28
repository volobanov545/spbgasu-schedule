#!/usr/bin/env python3
"""
Telegram-бот GASUCHKA (@gasu4ka_bot).

Архитектура:
  - Хостинг: Amvera (https://amvera.ru), persistent storage → /data
  - python-telegram-bot v20 (async, polling)
  - SQLite база через db.py, пароли шифруются Fernet
  - Парсинг портала — parse_journals.py (Playwright + Chromium)
  - Синхронизация расписания — sync_yandex.py (CalDAV)

Два вида клавиатур в Telegram:
  1. ReplyKeyboardMarkup  — «прилипает» к полю ввода, видна всегда пока бот её не уберёт.
                            Используется как главная навигация одобренного пользователя.
  2. InlineKeyboardMarkup — кнопки прикреплены к конкретному сообщению.
                            Используется для действий: одобрить/отклонить заявку,
                            настройки, подтверждение удаления и т.д.

Регистрация — ConversationHandler (reg_handler):
  /start или кнопка «Зарегистрироваться» → логин → пароль → Яндекс? → (логин ЯК → пароль ЯК) → ожидание

Подключение Яндекс.Календаря после регистрации — отдельный ConversationHandler (yc_handler):
  кнопка «Подключить» в настройках или /connect_yandex → логин ЯК → пароль ЯК → сохранить

Порядок app.add_handler() важен:
  ConversationHandler-ы добавляются первыми → они перехватывают апдейты раньше остальных.
  Глобальный CallbackQueryHandler(on_callback) добавляется после — ловит всё что не забрал
  ни один ConversationHandler.
"""

import asyncio
import html as html_mod
import json
import logging
import os
import re
import urllib.request
import warnings

import requests as _requests

try:
    from telegram.warnings import PTBUserWarning as _PTBUserWarning
except ImportError:
    _PTBUserWarning = UserWarning  # type: ignore[assignment,misc]

warnings.filterwarnings("ignore", message=".*per_message=False.*", category=_PTBUserWarning)
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from icalendar import Calendar

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    MenuButtonCommands,
)
try:
    from telegram import ChatAction           # ptb v21
except ImportError:
    from telegram.constants import ChatAction  # ptb v22+
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import caldav

from db import (
    init_db, add_user, set_yandex, set_student_name, set_attestations,
    set_reminder_minutes, set_quiet_until, clear_yandex,
    approve_user, ban_user, unban_user, remove_user,
    get_user, get_all_users,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN      = os.environ["TG_TOKEN"]
OWNER_ID   = int(os.environ["TG_OWNER_ID"])
TG_CHANNEL = os.environ.get("TG_CHANNEL", "")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# Состояния ConversationHandler регистрации (0–4)
WAIT_LOGIN, WAIT_PASSWORD, WAIT_YC_CHOICE, WAIT_YC_LOGIN, WAIT_YC_PASS = range(5)

# Состояния ConversationHandler подключения Яндекса после регистрации (10–11)
WAIT_YC2_LOGIN, WAIT_YC2_PASS = range(10, 12)

# Цикл времени напоминания: 0 (выкл) → 15 → 30 → 60 → 0
REMINDER_CYCLE = {0: 15, 15: 30, 30: 60, 60: 0}


# ─── Инициализация при старте бота ────────────────────────────────────────────

async def _post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start",    "Главное меню"),
        BotCommand("stats",    "Аттестации и посещаемость"),
        BotCommand("next",     "Следующая пара"),
        BotCommand("today",    "Расписание на сегодня"),
        BotCommand("week",     "Расписание на неделю"),
        BotCommand("quiet",    "Тихий режим на сегодня"),
        BotCommand("feedback", "Написать администратору"),
        BotCommand("help",     "Список команд"),
        BotCommand("cancel",   "Отменить"),
    ])
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await app.bot.set_my_description(
        "🎓 GASUCHKA — мониторинг учёбы в СПбГАСУ\n\n"
        "• Аттестации и посещаемость в реальном времени\n"
        "• Расписание в Яндекс.Календарь\n"
        "• Уведомления при изменениях расписания\n\n"
        "Нажми НАЧАТЬ чтобы подключиться."
    )
    await app.bot.set_my_short_description("Расписание и посещаемость СПбГАСУ")

    scheduler.add_job(morning_schedule,         "cron", hour=8, minute=0,  args=[app])
    scheduler.add_job(schedule_daily_reminders, "cron", hour=7, minute=50, args=[app])
    scheduler.start()
    await schedule_daily_reminders(app)
    log.info("APScheduler запущен")


async def _post_shutdown(app):
    scheduler.shutdown(wait=False)


# ─── Вспомогательные функции ──────────────────────────────────────────────────

PORTAL_AUTH_URL = "https://portal.spbgasu.ru/auth/"


def _test_portal(login: str, password: str) -> str | None:
    """
    Проверяет логин/пароль портала через HTTP (~2-3 сек, без Playwright).
    Returns None если OK или не удалось проверить, строку с ошибкой если точно неверный пароль.
    Логика: успех = оказались на /lk/ после POST; провал = форма логина снова видна.
    Сетевые ошибки — пропускаем, чтобы не блокировать регистрацию когда портал лежит.
    """
    try:
        s = _requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (compatible; GASUCHKA)"
        r = s.get(PORTAL_AUTH_URL, timeout=15)
        m = re.search(r'name=["\']sessid["\']\s+value=["\']([^"\']+)["\']', r.text)
        sessid = m.group(1) if m else ""
        r2 = s.post(
            PORTAL_AUTH_URL,
            data={
                "AUTH_FORM": "Y",
                "TYPE": "AUTH",
                "USER_LOGIN": login,
                "USER_PASSWORD": password,
                "USER_REMEMBER": "N",
                "sessid": sessid,
            },
            timeout=15,
            allow_redirects=True,
        )
        if "/lk/" in r2.url:
            return None
        # Форма логина снова видна — пароль точно неверный
        if 'name="USER_LOGIN"' in r2.text or "name='USER_LOGIN'" in r2.text:
            return "Неверный логин или пароль портала"
        return None  # непонятный ответ — пропускаем
    except (_requests.exceptions.Timeout, _requests.exceptions.ConnectionError):
        return None  # портал недоступен — не блокируем регистрацию
    except Exception as e:
        log.warning("portal check error: %s", e)
        return None


def _test_yandex(ylogin: str, ypass: str) -> str | None:
    try:
        client = caldav.DAVClient(
            url="https://caldav.yandex.ru",
            username=f"{ylogin}@yandex.ru",
            password=ypass,
        )
        client.principal()
        return None
    except Exception as e:
        return str(e)


YC_INSTRUCTION = (
    "📅 Чтобы подключить Яндекс.Календарь:\n\n"
    "1. Открой id.yandex.ru\n"
    "2. Безопасность → Пароли приложений\n"
    "3. Нажми «Создать пароль» → выбери «Другое»\n"
    "4. Скопируй пароль из 16 символов\n\n"
    "Введи свой логин Яндекса (без @yandex.ru):"
)

BTN_STATS    = "📋 Аттестации"
BTN_SCHEDULE = "📅 Расписание"
BTN_SETTINGS = "⚙️ Настройки"
BTN_USERS    = "👥 Пользователи"
KB_BTNS = {BTN_STATS, BTN_SCHEDULE, BTN_SETTINGS, BTN_USERS}


def reply_keyboard(is_owner: bool = False) -> ReplyKeyboardMarkup:
    row1 = [KeyboardButton(BTN_STATS), KeyboardButton(BTN_SCHEDULE)]
    row2 = [KeyboardButton(BTN_SETTINGS)]
    if is_owner:
        row2.append(KeyboardButton(BTN_USERS))
    return ReplyKeyboardMarkup(
        [row1, row2],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие...",
    )


# ─── Регистрация (ConversationHandler) ────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user = get_user(update.effective_user.id)

    if user:
        if user.get("banned"):
            await update.message.reply_text("🚫 Доступ закрыт.")
            return ConversationHandler.END
        if not user["approved"]:
            await update.message.reply_text(
                "⏳ <b>Заявка на рассмотрении</b>\n\n"
                f"Логин: <code>{user['login']}</code>\n\n"
                "Ожидай подтверждения от администратора.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        yc = "✅ подключён" if user["yandex_login"] else "не подключён"
        name_line = f"\n👤 {html_mod.escape(user['student_name'])}" if user.get("student_name") else ""
        await update.message.reply_text(
            "🎓 <b>GASUCHKA</b>\n\n"
            f"<code>{html_mod.escape(user['login'])}</code>{name_line}\n"
            f"📅 Яндекс.Календарь: {yc}",
            parse_mode="HTML",
            reply_markup=reply_keyboard(update.effective_user.id == OWNER_ID),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🎓 <b>GASUCHKA</b>\n\n"
        "Мониторинг расписания и посещаемости СПбГАСУ.\n"
        "Нужен аккаунт от студенческого портала.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register"),
        ]]),
    )
    return ConversationHandler.END


async def _start_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📝 <b>Регистрация — шаг 1 из 3</b>\n\n"
        "Введи логин от портала СПбГАСУ\n"
        "(студенческий номер, например <code>24001234</code>):",
        parse_mode="HTML",
    )
    return WAIT_LOGIN


async def got_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    login = update.message.text.strip()
    if not login or len(login) > 50:
        await update.message.reply_text("❌ Логин должен быть от 1 до 50 символов. Попробуй ещё раз:")
        return WAIT_LOGIN
    ctx.user_data["login"] = login
    await update.message.reply_text(
        "📝 <b>Регистрация — шаг 2 из 3</b>\n\n"
        "Введи пароль от портала:",
        parse_mode="HTML",
    )
    return WAIT_PASSWORD


async def got_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not password or len(password) > 256:
        await update.message.reply_text("❌ Пароль должен быть от 1 до 256 символов. Попробуй ещё раз:")
        return WAIT_PASSWORD

    login = ctx.user_data.get("login", "")
    checking = await update.message.reply_text("⏳ Проверяю логин и пароль портала...")
    err = await asyncio.to_thread(_test_portal, login, password)
    if err:
        await checking.edit_text(
            f"❌ {err}\n\n"
            f"Логин: <code>{html_mod.escape(login)}</code>\n"
            "Введи пароль ещё раз:",
            parse_mode="HTML",
        )
        return WAIT_PASSWORD

    await checking.delete()
    ctx.user_data["password"] = password
    await update.message.reply_text(
        "📝 <b>Регистрация — шаг 3 из 3</b>\n\n"
        "Хочешь подключить Яндекс.Календарь?\n"
        "Расписание будет автоматически появляться в твоём календаре.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [["✅ Да", "❌ Нет"]],
            one_time_keyboard=True,
            resize_keyboard=True,
            input_field_placeholder="Да или Нет...",
        ),
    )
    return WAIT_YC_CHOICE


async def got_yc_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() in ("да", "yes", "y", "д", "✅ да"):
        await update.message.reply_text(YC_INSTRUCTION)
        return WAIT_YC_LOGIN
    return await _finish_registration(update, ctx)


async def got_yc_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yc_login = update.message.text.strip()
    if not yc_login or len(yc_login) > 64:
        await update.message.reply_text("❌ Логин Яндекса должен быть до 64 символов. Попробуй ещё раз:")
        return WAIT_YC_LOGIN
    ctx.user_data["yc_login"] = yc_login
    await update.message.reply_text("Теперь введи пароль приложения (16 символов):")
    return WAIT_YC_PASS


async def got_yc_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yc_login = ctx.user_data.get("yc_login", "")
    yc_pass  = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text("⏳ Проверяю подключение к Яндекс.Календарю...")
    err = await asyncio.to_thread(_test_yandex, yc_login, yc_pass)
    if err:
        await update.message.reply_text(
            "❌ Не удалось подключиться. Проверь логин и пароль приложения.\n"
            "Введи логин Яндекса ещё раз:"
        )
        ctx.user_data.pop("yc_login", None)
        return WAIT_YC_LOGIN
    ctx.user_data["yc_pass"] = yc_pass
    return await _finish_registration(update, ctx)


async def _finish_registration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid      = update.effective_user.id
    login    = ctx.user_data.pop("login", "")
    password = ctx.user_data.pop("password", "")
    yc_login = ctx.user_data.pop("yc_login", None)
    yc_pass  = ctx.user_data.pop("yc_pass", None)
    username = update.effective_user.username or update.effective_user.first_name or str(tid)

    add_user(tid, login, password)
    if yc_login and yc_pass:
        set_yandex(tid, yc_login, yc_pass)

    await update.message.reply_text(
        "✅ Данные получены! Ожидай подтверждения от администратора.\n"
        "Как только тебя одобрят — сможешь пользоваться ботом."
    )
    yc_status = f"Яндекс: {yc_login}" if yc_login else "Яндекс: не подключён"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{tid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"deny:{tid}"),
    ]])
    await ctx.bot.send_message(
        chat_id=OWNER_ID,
        text=(
            f"🔔 Новая заявка на регистрацию:\n"
            f"Пользователь: @{username} (id: {tid})\n"
            f"Логин портала: {login}\n"
            f"{yc_status}"
        ),
        reply_markup=kb,
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ─── Яндекс.Календарь (отдельный ConversationHandler) ────────────────────────

async def cmd_connect_yandex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    query = update.callback_query
    if query:
        await query.answer()
        send = query.message.reply_text
    else:
        send = update.message.reply_text
    user = get_user(update.effective_user.id)
    if not user or user.get("banned"):
        return ConversationHandler.END
    if user["yandex_login"]:
        await send(
            f"📅 Яндекс.Календарь уже подключён (аккаунт: {user['yandex_login']}).\n\n"
            "Чтобы обновить пароль приложения — введи логин Яндекса заново.\n"
            "Или /cancel чтобы отменить."
        )
    else:
        await send(YC_INSTRUCTION)
    return WAIT_YC2_LOGIN


async def got_yc2_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yc_login = update.message.text.strip()
    if not yc_login or len(yc_login) > 64:
        await update.message.reply_text("❌ Логин Яндекса должен быть до 64 символов. Попробуй ещё раз:")
        return WAIT_YC2_LOGIN
    ctx.user_data["yc_login"] = yc_login
    await update.message.reply_text("Теперь введи пароль приложения:")
    return WAIT_YC2_PASS


async def got_yc2_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yc_login = ctx.user_data.get("yc_login", "")
    yc_pass  = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text("⏳ Проверяю подключение к Яндекс.Календарю...")
    err = await asyncio.to_thread(_test_yandex, yc_login, yc_pass)
    if err:
        await update.message.reply_text(
            "❌ Не удалось подключиться. Проверь логин и пароль приложения.\n"
            "Введи логин Яндекса ещё раз:"
        )
        ctx.user_data.pop("yc_login", None)
        return WAIT_YC2_LOGIN
    ctx.user_data.pop("yc_login", None)
    set_yandex(update.effective_user.id, yc_login, yc_pass)
    await update.message.reply_text(
        "✅ Яндекс.Календарь подключён!",
        reply_markup=reply_keyboard(update.effective_user.id == OWNER_ID),
    )
    return ConversationHandler.END


# ─── Статистика ───────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        send = query.message.reply_text
        tid  = query.from_user.id
    else:
        if update.effective_chat.type != "private":
            return
        send = update.message.reply_text
        tid  = update.effective_user.id

    user = get_user(tid)
    if not user:
        await send("Ты не зарегистрирован. Напиши /start.")
        return
    if user.get("banned"):
        await send("🚫 Доступ закрыт.")
        return
    if not user["approved"]:
        await send("⏳ Твоя заявка ещё не подтверждена администратором.")
        return

    await ctx.bot.send_chat_action(chat_id=tid, action=ChatAction.TYPING)
    loading_msg = await send("⏳ Загружаю данные с портала, подожди ~20 сек...")
    try:
        from parse_journals import parse_lk_quick
        data = await asyncio.to_thread(parse_lk_quick, user["login"], user["password"])
        text = _format_stats(data)
    except Exception as e:
        log.exception("stats error for %s", user["login"])
        await loading_msg.edit_text(f"❌ Ошибка при загрузке: {e}")
        return

    # Автосохраняем имя студента с портала если ещё не сохранено
    scraped_name = data.get("student_name", "")
    if scraped_name and not user.get("student_name"):
        set_student_name(tid, scraped_name)
        log.info("student_name автосохранён: %s → %s", user["login"], scraped_name)

    # Уведомление об изменениях в аттестациях
    current_att = data.get("attestations", {})
    if user.get("attestations_json"):
        try:
            old_att = json.loads(user["attestations_json"])
            changes = _compare_attestations(old_att, current_att)
            if changes:
                await ctx.bot.send_message(chat_id=tid, text=changes, parse_mode="HTML")
        except Exception:
            pass
    set_attestations(tid, json.dumps(current_att, ensure_ascii=False))

    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="refresh_stats"),
    ]])
    await loading_msg.edit_text(text, parse_mode="HTML", reply_markup=refresh_kb)

    if user["yandex_login"] and user["yandex_pass"]:
        try:
            from sync_yandex import sync_calendar
            ics = Path(__file__).parent / "schedule.ics"
            await asyncio.to_thread(sync_calendar, user["yandex_login"], user["yandex_pass"], ics)
        except Exception as e:
            log.warning("yandex sync error: %s", e)
            await send(
                "⚠️ Яндекс.Календарь: ошибка авторизации.\n"
                "Проверь пароль приложения — зайди в ⚙️ Настройки."
            )


def _fmt_grade(g: str) -> str:
    if not g or g == "—":
        return "⏳ —"
    if g.upper() in ("Н/А", "НА", "Н"):
        return f"❌ {g}"
    return f"✅ {g}"


def _compare_attestations(old: dict, new: dict) -> str:
    lines = []
    for subj, marks in new.items():
        old_marks = old.get(subj, {})
        for att_key, att_label in (("att1", "1-я"), ("att2", "2-я")):
            old_val = old_marks.get(att_key) or "—"
            new_val = marks.get(att_key) or "—"
            if old_val == new_val:
                continue
            if old_val == "—":
                lines.append(
                    f"📋 <b>{subj}</b>\n"
                    f"  {att_label} аттестация — {_fmt_grade(new_val)}"
                )
            else:
                lines.append(
                    f"📋 <b>{subj}</b>\n"
                    f"  {att_label} аттестация: {_fmt_grade(old_val)} → {_fmt_grade(new_val)}"
                )
    if not lines:
        return ""
    return "🔔 <b>Изменения в аттестациях</b>\n\n" + "\n\n".join(lines)


def _format_stats(data: dict) -> str:
    lines = []

    stats = data.get("stats", {})
    if stats:
        total   = stats.get("total_classes", "?")
        present = stats.get("present_pct", "?")
        absent  = stats.get("absent_pct", "?")
        lines.append(
            f"📊 <b>Посещаемость</b>\n"
            f"✅ <b>{present}%</b> присутствий  ·  ❌ <b>{absent}%</b> пропусков\n"
            f"📚 {total} занятий"
        )

    attestations = data.get("attestations", {})
    if attestations:
        lines.append("\n📋 <b>Аттестации</b>")
        for subj, marks in attestations.items():
            a1 = _fmt_grade(marks.get("att1") or "—")
            a2 = _fmt_grade(marks.get("att2") or "—")
            lines.append(f"\n<b>{subj}</b>\n  1-я: {a1}  ·  2-я: {a2}")
    else:
        lines.append("\n📋 <b>Аттестации:</b> данных нет")

    now = datetime.now(MOSCOW_TZ)
    lines.append(f"\n\n🕐 обновлено {now.strftime('%H:%M, %d.%m.%Y')}")

    return "\n".join(lines) if lines else "Нет данных."


# ─── Расписание: парсинг ICS + утреннее сообщение + напоминания ──────────────

ICS_URL  = "https://gitverse.ru/api/repos/volobanov5/spbgasu-schedule/raw/branch/main/schedule.ics"
DAYS_RU  = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def _get_lessons_for_date(target_date: date) -> list[dict]:
    ics_path = Path(__file__).parent / "schedule.ics"
    if ics_path.exists():
        raw = ics_path.read_bytes()
    else:
        with urllib.request.urlopen(ICS_URL, timeout=30) as r:
            raw = r.read()

    cal     = Calendar.from_ical(raw)
    lessons = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("dtstart")
        if not dtstart:
            continue
        dt = dtstart.dt
        if isinstance(dt, datetime):
            dt          = dt.replace(tzinfo=MOSCOW_TZ) if dt.tzinfo is None else dt.astimezone(MOSCOW_TZ)
            lesson_date = dt.date()
        else:
            lesson_date = dt
            dt          = datetime.combine(dt, datetime.min.time()).replace(tzinfo=MOSCOW_TZ)

        if lesson_date != target_date:
            continue

        dtend = component.get("dtend")
        if dtend:
            de = dtend.dt
            dt_end = (de.replace(tzinfo=MOSCOW_TZ) if de.tzinfo is None else de.astimezone(MOSCOW_TZ)) \
                if isinstance(de, datetime) else \
                datetime.combine(de, datetime.min.time()).replace(tzinfo=MOSCOW_TZ)
        else:
            dt_end = dt + timedelta(hours=2)

        lessons.append({
            "uid":         str(component.get("UID", "")),
            "summary":     str(component.get("SUMMARY", "Пара")),
            "dtstart":     dt,
            "dtend":       dt_end,
            "location":    str(component.get("LOCATION", "")),
            "description": str(component.get("DESCRIPTION", "")),
        })

    lessons.sort(key=lambda x: x["dtstart"])
    return lessons


def _format_day_schedule(target_date: date, lessons: list[dict]) -> str:
    day_name = DAYS_RU[target_date.weekday()]
    text = f"📅 <b>Расписание на {day_name}, {target_date.strftime('%d.%m')}</b>\n\n"
    for i, l in enumerate(lessons, 1):
        time_s = l["dtstart"].strftime("%H:%M")
        time_e = l["dtend"].strftime("%H:%M")
        text  += f"<b>{i}.</b> 🕐 {time_s}–{time_e}\n"
        text  += f"   📚 {l['summary']}\n"
        if l["location"]:
            text += f"   🚪 {l['location']}\n"
        if l["description"]:
            text += f"   👤 {l['description']}\n"
        text += "\n"
    return text.strip()


async def morning_schedule(app):
    if not TG_CHANNEL:
        log.warning("TG_CHANNEL не задан — утреннее расписание не отправлено")
        return
    today = date.today()
    try:
        lessons = _get_lessons_for_date(today)
    except Exception as e:
        log.error("Ошибка чтения расписания: %s", e)
        return
    if not lessons:
        await app.bot.send_message(
            chat_id=TG_CHANNEL,
            text="🌅 Сегодня пар нет",
            parse_mode="HTML",
        )
        log.info("Сегодня пар нет — отправлено в канал")
        return
    await app.bot.send_message(
        chat_id=TG_CHANNEL,
        text=_format_day_schedule(today, lessons),
        parse_mode="HTML",
    )
    log.info("Утреннее расписание отправлено: %d пар", len(lessons))


async def send_lesson_reminder(app, lesson: dict):
    """Напоминание за 30 мин до пары в канал."""
    if not TG_CHANNEL:
        return
    time_s = lesson["dtstart"].strftime("%H:%M")
    time_e = lesson["dtend"].strftime("%H:%M")
    text   = f"⏰ <b>Через 30 минут</b>\n\n📚 {lesson['summary']}\n🕐 {time_s}–{time_e}"
    if lesson["location"]:
        text += f"  ·  🚪 {lesson['location']}"
    if lesson["description"]:
        text += f"\n👤 {lesson['description']}"
    await app.bot.send_message(chat_id=TG_CHANNEL, text=text, parse_mode="HTML")


async def send_lesson_reminder_dm(app, lesson: dict, telegram_id: int, minutes: int):
    """Персональное напоминание в DM пользователю за N минут до пары."""
    user = get_user(telegram_id)
    if not user or user.get("banned"):
        return
    if user.get("quiet_until_date") == date.today().isoformat():
        return
    time_s = lesson["dtstart"].strftime("%H:%M")
    time_e = lesson["dtend"].strftime("%H:%M")
    text   = f"⏰ Через {minutes} мин\n\n📚 {lesson['summary']}\n🕐 {time_s}–{time_e}"
    if lesson["location"]:
        text += f"  ·  🚪 {lesson['location']}"
    if lesson["description"]:
        text += f"\n👤 {lesson['description']}"
    try:
        await app.bot.send_message(chat_id=telegram_id, text=text)
    except Exception as e:
        log.warning("DM reminder error for %s: %s", telegram_id, e)


async def schedule_daily_reminders(app):
    if not TG_CHANNEL:
        return
    today = date.today()
    try:
        lessons = _get_lessons_for_date(today)
    except Exception as e:
        log.error("Ошибка расписания для напоминаний: %s", e)
        return
    now       = datetime.now(MOSCOW_TZ)
    scheduled = 0

    # Напоминание в канал за 30 мин
    for lesson in lessons:
        remind_dt = lesson["dtstart"] - timedelta(minutes=30)
        if remind_dt > now:
            scheduler.add_job(
                send_lesson_reminder,
                "date",
                run_date=remind_dt,
                args=[app, lesson],
                id=f"reminder_{lesson['uid']}",
                replace_existing=True,
            )
            scheduled += 1

    # Персональные DM-напоминания
    users = get_all_users()
    for u in users:
        rm = u.get("reminder_minutes", 0)
        if not rm or not u["approved"] or u.get("banned"):
            continue
        for lesson in lessons:
            remind_dt = lesson["dtstart"] - timedelta(minutes=rm)
            if remind_dt > now:
                scheduler.add_job(
                    send_lesson_reminder_dm,
                    "date",
                    run_date=remind_dt,
                    args=[app, lesson, u["telegram_id"], rm],
                    id=f"reminder_dm_{lesson['uid']}_{u['telegram_id']}",
                    replace_existing=True,
                )

    if scheduled:
        log.info("Запланировано напоминаний в канал: %d", scheduled)


# ─── Команды расписания ───────────────────────────────────────────────────────

async def cmd_schedule_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопка «📅 Расписание» — открывает inline-меню."""
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user or not user["approved"] or user.get("banned"):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня",         callback_data="sched_today")],
        [InlineKeyboardButton("📆 На неделю",        callback_data="sched_week")],
        [InlineKeyboardButton("▶️ Следующая пара",   callback_data="sched_next")],
    ])
    await update.message.reply_text("📅 <b>Расписание</b>", parse_mode="HTML", reply_markup=kb)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user or not user["approved"] or user.get("banned"):
        return
    await _send_today(update.message.reply_text)


async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user or not user["approved"] or user.get("banned"):
        return
    await _send_next(update.message.reply_text)


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user or not user["approved"] or user.get("banned"):
        return
    await _send_week(update.message.reply_text)


async def _send_today(send):
    today = date.today()
    try:
        lessons = _get_lessons_for_date(today)
    except Exception as e:
        await send(f"❌ Ошибка чтения расписания: {e}")
        return
    if not lessons:
        await send("🌅 Сегодня пар нет")
        return
    await send(_format_day_schedule(today, lessons), parse_mode="HTML")


async def _send_next(send):
    now = datetime.now(MOSCOW_TZ)
    try:
        for delta in range(7):
            d = date.today() + timedelta(days=delta)
            lessons = _get_lessons_for_date(d)
            for l in lessons:
                if l["dtstart"] > now:
                    time_s   = l["dtstart"].strftime("%H:%M")
                    time_e   = l["dtend"].strftime("%H:%M")
                    day_name = DAYS_RU[d.weekday()]
                    text     = f"▶️ <b>Следующая пара</b>\n\n"
                    if delta > 0:
                        text += f"📅 {day_name}, {d.strftime('%d.%m')}\n"
                    text += f"📚 {l['summary']}\n🕐 {time_s}–{time_e}"
                    if l["location"]:
                        text += f"  ·  🚪 {l['location']}"
                    if l["description"]:
                        text += f"\n👤 {l['description']}"
                    await send(text, parse_mode="HTML")
                    return
    except Exception as e:
        await send(f"❌ Ошибка чтения расписания: {e}")
        return
    await send("Ближайших пар не найдено.")


async def _send_week(send):
    today  = date.today()
    monday = today - timedelta(days=today.weekday())
    parts  = []
    try:
        for i in range(7):
            d       = monday + timedelta(days=i)
            lessons = _get_lessons_for_date(d)
            if lessons:
                parts.append(_format_day_schedule(d, lessons))
    except Exception as e:
        await send(f"❌ Ошибка чтения расписания: {e}")
        return
    if not parts:
        await send("На этой неделе пар нет.")
        return
    # Разбиваем на сообщения если превышает лимит Telegram
    chunk = ""
    for part in parts:
        candidate = chunk + ("\n\n" if chunk else "") + part
        if len(candidate) > 3800:
            await send(chunk, parse_mode="HTML")
            chunk = part
        else:
            chunk = candidate
    if chunk:
        await send(chunk, parse_mode="HTML")


# ─── Настройки ───────────────────────────────────────────────────────────────

def _render_settings(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    if user["yandex_login"]:
        yc_btn    = InlineKeyboardButton("📅 Отключить Яндекс.Календарь", callback_data="disconnect_yc")
        yc_status = f"подключён ({user['yandex_login']})"
    else:
        yc_btn    = InlineKeyboardButton("📅 Подключить Яндекс.Календарь", callback_data="connect_yandex")
        yc_status = "не подключён"

    rm       = user.get("reminder_minutes", 0)
    rm_label = f"{rm} мин" if rm else "выкл"
    is_quiet = user.get("quiet_until_date") == date.today().isoformat()
    quiet_label = "🔕 Тихий режим: вкл" if is_quiet else "🔔 Тихий режим: выкл"

    kb = InlineKeyboardMarkup([
        [yc_btn],
        [InlineKeyboardButton(f"⏰ Напоминания: {rm_label}", callback_data="reminder_cycle")],
        [InlineKeyboardButton(quiet_label,                   callback_data="toggle_quiet")],
        [InlineKeyboardButton("❌ Удалить аккаунт",          callback_data="unregister")],
    ])
    return f"⚙️ Настройки\n\nЯндекс.Календарь: {yc_status}", kb


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user or not user["approved"] or user.get("banned"):
        return
    text, kb = _render_settings(user)
    await update.message.reply_text(text, reply_markup=kb)


# ─── Тихий режим ─────────────────────────────────────────────────────────────

async def cmd_quiet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user or not user["approved"] or user.get("banned"):
        return
    tid       = update.effective_user.id
    today_str = date.today().isoformat()
    if user.get("quiet_until_date") == today_str:
        set_quiet_until(tid, None)
        await update.message.reply_text("🔔 Тихий режим выключен.")
    else:
        set_quiet_until(tid, today_str)
        await update.message.reply_text("🔕 Тихий режим на сегодня включён. Личные напоминания не придут.")


# ─── Удаление аккаунта ────────────────────────────────────────────────────────

async def cmd_unregister(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        send = query.message.reply_text
        tid  = query.from_user.id
    else:
        send = update.message.reply_text
        tid  = update.effective_user.id

    user     = get_user(tid)
    cal_note = "\n📅 Календарь «СПбГАСУ» в Яндексе будет удалён." if (user and user.get("yandex_login")) else ""
    await send(
        f"⚠️ Удалить аккаунт? Все данные (логин, пароль, Яндекс) будут удалены.{cal_note}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Да, удалить", callback_data="confirm_unregister"),
            InlineKeyboardButton("↩️ Отмена",      callback_data="cancel_action"),
        ]]),
    )


def _tid_from_data(data: str) -> int | None:
    try:
        return int(data.split(":")[1])
    except (IndexError, ValueError):
        return None


# ─── Глобальный обработчик inline-кнопок ─────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    tid   = query.from_user.id

    if data == "stats":
        await query.answer()
        await cmd_stats(update, ctx)
        return

    if data in ("sched_today", "sched_week", "sched_next"):
        await query.answer()
        send = query.message.reply_text
        if data == "sched_today":
            await _send_today(send)
        elif data == "sched_week":
            await _send_week(send)
        else:
            await _send_next(send)
        return

    if data == "reminder_cycle":
        user = get_user(tid)
        if not user:
            await query.answer()
            return
        current  = user.get("reminder_minutes", 0)
        next_val = REMINDER_CYCLE.get(current, 0)
        set_reminder_minutes(tid, next_val)
        label = f"{next_val} мин" if next_val else "выкл"
        await query.answer(f"⏰ Напоминания: {label}")
        user = get_user(tid)
        text, kb = _render_settings(user)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data == "toggle_quiet":
        user      = get_user(tid)
        today_str = date.today().isoformat()
        if user and user.get("quiet_until_date") == today_str:
            set_quiet_until(tid, None)
            await query.answer("🔔 Тихий режим выключен")
        else:
            set_quiet_until(tid, today_str)
            await query.answer("🔕 Тихий режим на сегодня включён")
        user = get_user(tid)
        text, kb = _render_settings(user)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data == "unregister":
        await query.answer()
        user = get_user(tid)
        cal_note = "\n📅 Календарь «СПбГАСУ» в Яндексе будет удалён." if (user and user.get("yandex_login")) else ""
        await query.message.reply_text(
            f"⚠️ Удалить аккаунт? Все данные (логин, пароль, Яндекс) будут удалены.{cal_note}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Да, удалить", callback_data="confirm_unregister"),
                InlineKeyboardButton("↩️ Отмена",      callback_data="cancel_action"),
            ]])
        )
        return

    if data == "confirm_unregister":
        user    = get_user(tid)
        cal_msg = ""
        if user and user.get("yandex_login") and user.get("yandex_pass"):
            try:
                from sync_yandex import delete_yandex_calendar
                deleted = await asyncio.to_thread(
                    delete_yandex_calendar, user["yandex_login"], user["yandex_pass"]
                )
                cal_msg = " Календарь удалён ✓" if deleted else ""
            except Exception as e:
                log.warning("calendar delete error: %s", e)
        remove_user(tid)
        await query.answer(text=f"Аккаунт удалён.{cal_msg}", show_alert=True)
        await query.edit_message_text("Аккаунт удалён. Напиши /start чтобы зарегистрироваться снова.")
        return

    if data == "cancel_action":
        await query.answer()
        await query.edit_message_text("Отменено.")
        return

    if data == "refresh_stats":
        await query.answer("🔄 Обновляю...")
        await cmd_stats(update, ctx)
        return

    if data == "disconnect_yc":
        await query.answer()
        user = get_user(tid)
        if user and user.get("yandex_login") and user.get("yandex_pass"):
            try:
                from sync_yandex import delete_yandex_calendar
                await asyncio.to_thread(
                    delete_yandex_calendar, user["yandex_login"], user["yandex_pass"]
                )
            except Exception as e:
                log.warning("calendar delete error: %s", e)
        clear_yandex(tid)
        await query.edit_message_text("📅 Яндекс.Календарь отключён. Календарь «СПбГАСУ» удалён из Яндекса.")
        return

    # Далее — только для OWNER_ID
    await query.answer()
    if tid != OWNER_ID:
        return

    if data.startswith("owner_remove:"):
        owner_tid = _tid_from_data(data)
        if owner_tid is None:
            return
        user = get_user(owner_tid)
        if user and user.get("yandex_login") and user.get("yandex_pass"):
            try:
                from sync_yandex import delete_yandex_calendar
                await asyncio.to_thread(
                    delete_yandex_calendar, user["yandex_login"], user["yandex_pass"]
                )
            except Exception as e:
                log.warning("calendar delete error for %s: %s", owner_tid, e)
        remove_user(owner_tid)
        await query.edit_message_text(query.message.text + "\n\n🗑 Удалён")
        try:
            await ctx.bot.send_message(chat_id=owner_tid, text="Твой аккаунт удалён администратором.")
        except Exception:
            pass
        return

    if data.startswith("approve:"):
        target = _tid_from_data(data)
        if target is None:
            return
        approve_user(target)
        await query.edit_message_text(query.message.text + "\n\n✅ Одобрено")
        try:
            await ctx.bot.send_message(
                chat_id=target,
                text="✅ Твоя заявка одобрена! Можешь пользоваться ботом.",
                reply_markup=reply_keyboard(target == OWNER_ID),
            )
        except Exception:
            pass

    elif data.startswith("deny:"):
        target = _tid_from_data(data)
        if target is None:
            return
        remove_user(target)
        await query.edit_message_text(query.message.text + "\n\n❌ Отклонено и удалено")
        try:
            await ctx.bot.send_message(chat_id=target, text="❌ Твоя заявка отклонена.")
        except Exception:
            pass

    elif data.startswith("ban:"):
        target = _tid_from_data(data)
        if target is None:
            return
        ban_user(target)
        await query.edit_message_text(query.message.text + "\n\n🚫 Заблокирован")
        try:
            await ctx.bot.send_message(chat_id=target, text="🚫 Твой доступ заблокирован.")
        except Exception:
            pass

    elif data.startswith("unban:"):
        target = _tid_from_data(data)
        if target is None:
            return
        unban_user(target)
        await query.edit_message_text(query.message.text + "\n\n✅ Разблокирован")


# ─── Админ-команды ─────────────────────────────────────────────────────────────

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    approve_user(target)
    await update.message.reply_text(f"✅ {target} одобрен.")
    try:
        await ctx.bot.send_message(chat_id=target, text="✅ Твоя заявка одобрена! Напиши /start.")
    except Exception:
        pass


async def cmd_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    remove_user(target)
    await update.message.reply_text(f"❌ {target} отклонён.")
    try:
        await ctx.bot.send_message(chat_id=target, text="❌ Твоя заявка отклонена.")
    except Exception:
        pass


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    ban_user(target)
    await update.message.reply_text(f"🚫 {target} заблокирован.")
    try:
        await ctx.bot.send_message(chat_id=target, text="🚫 Твой доступ заблокирован.")
    except Exception:
        pass


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    unban_user(target)
    await update.message.reply_text(f"✅ {target} разблокирован.")


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет пользователей.")
        return
    await update.message.reply_text(f"👥 Пользователей: {len(users)}")
    for u in users:
        target = u["telegram_id"]
        if u.get("banned"):
            status     = "🚫 Заблокирован"
            toggle_btn = InlineKeyboardButton("✅ Разбанить",     callback_data=f"unban:{target}")
        elif u["approved"]:
            status     = "✅ Активен"
            toggle_btn = InlineKeyboardButton("🚫 Заблокировать", callback_data=f"ban:{target}")
        else:
            status     = "⏳ Ожидает подтверждения"
            toggle_btn = InlineKeyboardButton("✅ Одобрить",       callback_data=f"approve:{target}")
        yc    = "📅 Яндекс подключён" if u["yandex_login"] else "без Яндекса"
        sname = u.get("student_name") or "—"
        text  = f"{status}\nID: {target} | Портал: {u['login']} | {sname}\n{yc}"
        kb    = InlineKeyboardMarkup([[
            toggle_btn,
            InlineKeyboardButton("🗑 Удалить", callback_data=f"owner_remove:{target}"),
        ]])
        await update.message.reply_text(text, reply_markup=kb)


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    remove_user(target)
    await update.message.reply_text("✅ Удалён.")


async def cmd_announce(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Рассылка всем одобренным пользователям."""
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    text  = " ".join(ctx.args)
    users = get_all_users()
    sent  = 0
    for u in users:
        if u["approved"] and not u.get("banned"):
            try:
                await ctx.bot.send_message(u["telegram_id"], f"📢 {text}")
                sent += 1
            except Exception:
                pass
    await update.message.reply_text(f"✅ Отправлено {sent} пользователям.")


async def cmd_sendhtml(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    owner = get_user(OWNER_ID)
    login = owner["login"] if owner else "unknown"
    path  = Path(os.environ.get("DATA_DIR", ".")) / f"debug_lk_{login}.html"
    if not path.exists():
        await update.message.reply_text(f"{path.name} не найден — сначала вызови /stats")
        return
    await update.message.reply_document(document=open(path, "rb"), filename=path.name)


async def cmd_sendpng(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    owner = get_user(OWNER_ID)
    login = owner["login"] if owner else "unknown"
    path  = Path(os.environ.get("DATA_DIR", ".")) / f"debug_lk_{login}.png"
    if not path.exists():
        await update.message.reply_text(f"{path.name} не найден — сначала вызови /stats")
        return
    await update.message.reply_photo(photo=open(path, "rb"))


# ─── Помощь и обратная связь ─────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "🤖 <b>GASUCHKA — команды</b>\n\n"
        "/stats — посещаемость и аттестации\n"
        "/next — следующая пара\n"
        "/today — расписание на сегодня\n"
        "/week — расписание на неделю\n"
        "/quiet — тихий режим на сегодня (без личных напоминаний)\n"
        "/feedback — написать администратору\n"
        "/cancel — отменить текущее действие",
        parse_mode="HTML",
    )


async def cmd_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not ctx.args:
        if update.effective_chat.type == "private":
            await update.message.reply_text("Напиши: /feedback <сообщение>")
        return
    user    = get_user(update.effective_user.id)
    from_id = update.effective_user.id
    name    = user.get("student_name") or (user["login"] if user else str(from_id))
    text    = " ".join(ctx.args)
    await ctx.bot.send_message(OWNER_ID, f"📬 Feedback от {name} ({from_id}):\n\n{text}")
    await update.message.reply_text("✉️ Отправлено администратору.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).post_init(_post_init).post_shutdown(_post_shutdown).build()

    not_kb = ~filters.Regex("^(" + "|".join(re.escape(b) for b in KB_BTNS) + ")$")

    reg_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start, filters=filters.ChatType.PRIVATE),
            CallbackQueryHandler(_start_register, pattern="^register$"),
        ],
        states={
            WAIT_LOGIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND & not_kb, got_login)],
            WAIT_PASSWORD:  [MessageHandler(filters.TEXT & ~filters.COMMAND & not_kb, got_password)],
            WAIT_YC_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND & not_kb, got_yc_choice)],
            WAIT_YC_LOGIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND & not_kb, got_yc_login)],
            WAIT_YC_PASS:   [MessageHandler(filters.TEXT & ~filters.COMMAND & not_kb, got_yc_pass)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    yc_handler = ConversationHandler(
        entry_points=[
            CommandHandler("connect_yandex", cmd_connect_yandex, filters=filters.ChatType.PRIVATE),
            CallbackQueryHandler(cmd_connect_yandex, pattern="^connect_yandex$"),
        ],
        states={
            WAIT_YC2_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND & not_kb, got_yc2_login)],
            WAIT_YC2_PASS:  [MessageHandler(filters.TEXT & ~filters.COMMAND & not_kb, got_yc2_pass)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(reg_handler)
    app.add_handler(yc_handler)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("next",       cmd_next))
    app.add_handler(CommandHandler("today",      cmd_today))
    app.add_handler(CommandHandler("week",       cmd_week))
    app.add_handler(CommandHandler("quiet",      cmd_quiet))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("feedback",   cmd_feedback))
    app.add_handler(CommandHandler("announce",   cmd_announce))
    app.add_handler(CommandHandler("unregister", cmd_unregister))
    app.add_handler(CommandHandler("approve",    cmd_approve))
    app.add_handler(CommandHandler("deny",       cmd_deny))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CommandHandler("users",      cmd_users))
    app.add_handler(CommandHandler("remove",     cmd_remove))
    app.add_handler(CommandHandler("sendhtml",   cmd_sendhtml))
    app.add_handler(CommandHandler("sendpng",    cmd_sendpng))

    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_STATS)}$")    & filters.ChatType.PRIVATE, cmd_stats))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_SCHEDULE)}$") & filters.ChatType.PRIVATE, cmd_schedule_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_SETTINGS)}$") & filters.ChatType.PRIVATE, cmd_settings))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_USERS)}$")    & filters.ChatType.PRIVATE, cmd_users))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
