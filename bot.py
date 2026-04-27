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
import logging
import os
import re
import urllib.request
import warnings

from telegram.warnings import PTBUserWarning

# per_message=False (умолчание) — правильный выбор: entry_point — callback-кнопка,
# все последующие состояния — текстовые сообщения. PTBUserWarning здесь ложный.
warnings.filterwarnings("ignore", message=".*per_message=False.*", category=PTBUserWarning)
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
    BotCommand,           # нужен для set_my_commands() в _post_init
    MenuButtonCommands,   # кнопка «≡» в поле ввода — видна даже в пустом чате
    ChatAction,           # константы для send_chat_action (typing, upload_document и т.д.)
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import caldav  # нужен только для _test_yandex — проверки подключения перед сохранением

from db import (
    init_db, add_user, set_yandex, clear_yandex,
    approve_user, ban_user, unban_user, remove_user,
    get_user, get_all_users,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx по умолчанию логирует полные URL включая токен бота — подавляем до WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN      = os.environ["TG_TOKEN"]
OWNER_ID   = int(os.environ["TG_OWNER_ID"])   # 796071683 — только он видит /users, /approve и пр.
TG_CHANNEL = os.environ.get("TG_CHANNEL", "") # @gasu4ka — канал для расписания и напоминаний

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Глобальный планировщик задач (утреннее расписание + напоминания).
# AsyncIOScheduler работает в том же event loop что и бот — не нужен отдельный поток.
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# Состояния ConversationHandler регистрации (0–4)
WAIT_LOGIN, WAIT_PASSWORD, WAIT_YC_CHOICE, WAIT_YC_LOGIN, WAIT_YC_PASS = range(5)

# Состояния ConversationHandler подключения Яндекса после регистрации (10–11)
# Намеренно начинаем с 10, чтобы не пересекаться с состояниями reg_handler
WAIT_YC2_LOGIN, WAIT_YC2_PASS = range(10, 12)


# ─── Инициализация при старте бота ────────────────────────────────────────────

async def _post_init(app):
    """
    Вызывается один раз после запуска бота (до начала polling).
    Настраивает:
      - команды в меню (≡) — видны пользователям в BotFather-style списке
      - MenuButtonCommands — заменяет скрепку в поле ввода на кнопку «≡»,
        которая открывает список команд ДАЖЕ В ПУСТОМ ЧАТЕ
      - описание бота — текст, который видит пользователь при первом открытии
        или после очистки истории чата
    """
    await app.bot.set_my_commands([
        BotCommand("start",  "Главное меню"),
        BotCommand("stats",  "Аттестации и посещаемость"),
        BotCommand("cancel", "Отменить"),
    ])
    # MenuButtonCommands — стандартная кнопка-гамбургер ≡ рядом с полем ввода.
    # Без этого вызова там была бы скрепка для вложений.
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await app.bot.set_my_description(
        "🎓 GASUCHKA — мониторинг учёбы в СПбГАСУ\n\n"
        "• Аттестации и посещаемость в реальном времени\n"
        "• Расписание в Яндекс.Календарь\n"
        "• Уведомления при изменениях расписания\n\n"
        "Нажми НАЧАТЬ чтобы подключиться."
    )
    await app.bot.set_my_short_description("Расписание и посещаемость СПбГАСУ")

    # Ежедневные задачи через APScheduler.
    # В 08:00 — отправляем расписание на сегодня в канал.
    # В 07:50 — планируем напоминания за 30 мин до каждой пары.
    scheduler.add_job(morning_schedule,         "cron", hour=8, minute=0,  args=[app])
    scheduler.add_job(schedule_daily_reminders, "cron", hour=7, minute=50, args=[app])
    scheduler.start()
    # Если бот перезапустился в течение дня — сразу ставим напоминания на оставшиеся пары
    await schedule_daily_reminders(app)
    log.info("APScheduler запущен")


async def _post_shutdown(app):
    """Корректно останавливаем планировщик при выключении бота."""
    scheduler.shutdown(wait=False)


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _test_yandex(ylogin: str, ypass: str) -> str | None:
    """
    Синхронная проверка подключения к Яндекс.Календарю через CalDAV.
    Вызывается через asyncio.to_thread() чтобы не блокировать event loop.
    Возвращает None если всё ОК, иначе текст ошибки.

    Яндекс CalDAV требует пароль ПРИЛОЖЕНИЯ (16 символов из id.yandex.ru),
    а НЕ основной пароль аккаунта — это важно объяснять пользователям.
    """
    try:
        client = caldav.DAVClient(
            url="https://caldav.yandex.ru",
            username=f"{ylogin}@yandex.ru",
            password=ypass,
        )
        client.principal()  # делает реальный HTTP-запрос, проверяет авторизацию
        return None
    except Exception as e:
        return str(e)


# Инструкция по получению пароля приложения — показывается при каждом запросе ЯК
YC_INSTRUCTION = (
    "📅 Чтобы подключить Яндекс.Календарь:\n\n"
    "1. Открой id.yandex.ru\n"
    "2. Безопасность → Пароли приложений\n"
    "3. Нажми «Создать пароль» → выбери «Другое»\n"
    "4. Скопируй пароль из 16 символов\n\n"
    "Введи свой логин Яндекса (без @yandex.ru):"
)

# Тексты кнопок постоянной клавиатуры — вынесены в константы,
# потому что они же используются в фильтре not_kb (см. main()).
# Если изменить текст кнопки здесь — фильтр и обработчик обновятся автоматически.
BTN_STATS    = "📋 Аттестации"
BTN_USERS    = "👥 Пользователи"   # показывается только OWNER_ID
BTN_SETTINGS = "⚙️ Настройки"
KB_BTNS = {BTN_STATS, BTN_USERS, BTN_SETTINGS}  # множество для быстрой проверки в not_kb


def reply_keyboard(is_owner: bool = False) -> ReplyKeyboardMarkup:
    """
    Постоянная клавиатура снизу экрана для одобренных пользователей.
    resize_keyboard=True — кнопки компактные, не занимают пол-экрана.
    input_field_placeholder — подсказка в поле ввода пока клавиатура активна.
    Кнопка «👥 Пользователи» добавляется только владельцу (OWNER_ID).
    Для обычного: одна строка [Аттестации | Настройки].
    Для овнера: [Аттестации | Пользователи] / [Настройки].
    """
    if is_owner:
        rows = [
            [KeyboardButton(BTN_STATS), KeyboardButton(BTN_SETTINGS)],
            [KeyboardButton(BTN_USERS)],
        ]
    else:
        rows = [[KeyboardButton(BTN_STATS), KeyboardButton(BTN_SETTINGS)]]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        input_field_placeholder="Выбери действие...",
    )


# ─── Регистрация (ConversationHandler) ────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Точка входа: /start (только в личном чате).
    Три сценария:
      1. Пользователь в БД + одобрен → показываем меню с reply_keyboard
      2. Пользователь в БД, но не одобрен / забанен → сообщение о статусе
      3. Пользователя нет → приветственный экран с кнопкой «Зарегистрироваться»

    ВАЖНО: возвращаем ConversationHandler.END во всех ветках кроме новой регистрации,
    потому что cmd_start — entry_point ConversationHandler. Если вернуть END,
    разговор не начнётся и пользователь не попадёт в состояние WAIT_LOGIN.
    Для новых пользователей тоже возвращаем END — разговор начинается через
    отдельный callback «register» → _start_register, который возвращает WAIT_LOGIN.
    """
    if update.effective_chat.type != "private":
        return  # игнорируем групповые чаты

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
        await update.message.reply_text(
            "🎓 <b>GASUCHKA</b>\n\n"
            f"👤 <code>{html_mod.escape(user['login'])}</code>\n"
            f"📅 Яндекс.Календарь: {yc}",
            parse_mode="HTML",
            reply_markup=reply_keyboard(update.effective_user.id == OWNER_ID),
        )
        return ConversationHandler.END

    # Новый пользователь — показываем inline-кнопку вместо слепого «введи логин»
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
    """
    Entry point ConversationHandler через inline-кнопку «Зарегистрироваться».
    Срабатывает на callback_data="register".
    query.answer() — обязательно для inline-кнопок, убирает «часики» у кнопки.
    Возвращает WAIT_LOGIN — следующее сообщение пользователя попадёт в got_login().
    """
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
    """Сохраняем логин в ctx.user_data (временное хранилище на время диалога)."""
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
    """
    Сохраняем пароль, удаляем сообщение с ним — пароль не должен висеть в истории.
    one_time_keyboard=True — клавиатура исчезает сразу после нажатия.
    Кнопки «✅ Да» / «❌ Нет» избавляют от необходимости что-то печатать.
    """
    password = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass  # нет прав на удаление — не критично
    if not password or len(password) > 256:
        await update.message.reply_text("❌ Пароль должен быть от 1 до 256 символов. Попробуй ещё раз:")
        return WAIT_PASSWORD
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
    """Разветвление: да → запрашиваем данные Яндекса, нет → сразу завершаем регистрацию."""
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
    """
    Проверяем CalDAV перед сохранением — не хочется хранить заведомо битые данные.
    asyncio.to_thread() нужен потому что caldav делает синхронные HTTP-запросы,
    а мы не должны блокировать async event loop бота.
    При ошибке — возвращаем пользователя на ввод логина (он мог ошибиться в нём).
    Пароль приложения удаляем из чата — он не должен висеть в истории.
    """
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
    """
    Финальный шаг регистрации:
      1. Сохраняем логин/пароль портала в БД (пароль шифруется Fernet в db.py)
      2. Сохраняем данные Яндекса если были введены
      3. Уведомляем пользователя — он ждёт одобрения
      4. Отправляем владельцу карточку заявки с кнопками одобрить/отклонить

    ctx.user_data.pop() — очищаем временные данные после использования,
    чтобы не оставлять пароли в памяти дольше необходимого.
    """
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
    """Экстренный выход из любого ConversationHandler. Очищаем user_data."""
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ─── Яндекс.Календарь (отдельный ConversationHandler) ────────────────────────

async def cmd_connect_yandex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Entry point yc_handler — подключение/обновление Яндекс.Календаря уже
    зарегистрированным пользователем (не в процессе регистрации).
    Вызывается двумя способами:
      - callback_data="connect_yandex" из кнопки «Подключить ЯК» в настройках
      - командой /connect_yandex

    Если ЯК уже подключён — предлагаем обновить пароль приложения
    (они протухают или пользователь мог его пересоздать).
    """
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
    """
    Проверяем CalDAV и сохраняем. После подключения отправляем reply_keyboard —
    это нужно чтобы клавиатура обновилась (хотя визуально она не меняется,
    отправка гарантирует что она точно есть у пользователя).
    Пароль приложения удаляем из чата.
    """
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
    """
    Основная функция бота — парсит портал и выводит аттестации + посещаемость.

    Принимает апдейты двух типов:
      - Message (кнопка «📋 Аттестации» или команда /stats)
      - CallbackQuery (inline-кнопка "stats" из старых меню)

    parse_lk_quick() запускает Playwright, логинится на портал и парсит /lk/.
    Работает ~20 секунд — браузер, логин, ожидание Vue-компонента.

    Портал: Bitrix CMS + Vue SPA. После Bitrix-логина на /auth/ нужно перейти
    на /lk/ — только тогда Vue подхватывает сессию. Сессия НЕ кешируется —
    всегда делаем свежий логин, иначе протухшие куки вызывают показ формы
    логина вместо дашборда, и аттестации не парсятся.

    После получения данных — тихая синхронизация с Яндекс.Календарём.
    Сообщение об успехе не показываем — только об ошибке авторизации.
    """
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

    # send_chat_action показывает «печатает...» в шапке чата пока грузятся данные.
    # Telegram не поддерживает кастомные тексты action — только системные варианты.
    # ChatAction.TYPING — единственный подходящий для «готовлю ответ».
    await ctx.bot.send_chat_action(chat_id=tid, action=ChatAction.TYPING)

    # Отправляем «загружаю» и сохраняем ссылку — потом отредактируем вместо нового сообщения.
    loading_msg = await send("⏳ Загружаю данные с портала, подожди ~20 сек...")
    try:
        from parse_journals import parse_lk_quick
        data = await asyncio.to_thread(parse_lk_quick, user["login"], user["password"])
        text = _format_stats(data)
    except Exception as e:
        log.exception("stats error for %s", user["login"])
        await loading_msg.edit_text(f"❌ Ошибка при загрузке: {e}")
        return

    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="refresh_stats"),
    ]])
    await loading_msg.edit_text(text, parse_mode="HTML", reply_markup=refresh_kb)

    # Синхронизация Яндекс.Календаря — тихо, без сообщения об успехе.
    # Если пароль протух — сообщаем и просим обновить в настройках.
    if user["yandex_login"] and user["yandex_pass"]:
        try:
            from sync_yandex import sync_calendar
            from pathlib import Path
            ics = Path(__file__).parent / "schedule.ics"
            await asyncio.to_thread(sync_calendar, user["yandex_login"], user["yandex_pass"], ics)
        except Exception as e:
            log.warning("yandex sync error: %s", e)
            await send(
                "⚠️ Яндекс.Календарь: ошибка авторизации.\n"
                "Проверь пароль приложения — зайди в ⚙️ Настройки."
            )


def _fmt_grade(g: str) -> str:
    """Форматирует оценку аттестации с эмодзи-индикатором."""
    if not g or g == "—":
        return "⏳ —"
    if g.upper() in ("Н/А", "НА", "Н"):
        return f"❌ {g}"
    return f"✅ {g}"


def _format_stats(data: dict) -> str:
    """
    Форматирует словарь из parse_lk_quick() в HTML для Telegram.
    data = {
        "stats": {"total_classes": int, "present_pct": float, "absent_pct": float, ...},
        "attestations": {"Предмет": {"att1": "А", "att2": "—"}, ...},
        "absences": {}  # в quick-режиме всегда пустой — журналы не обходим
    }
    Оценки: А → ✅, Н/А → ❌, — → ⏳ (ещё не выставлена).
    """
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

ICS_URL = "https://gitverse.ru/api/repos/volobanov5/spbgasu-schedule/raw/branch/main/schedule.ics"
DAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def _get_lessons_for_date(target_date: date) -> list[dict]:
    """
    Читает schedule.ics из локального файла (бот берёт его из репо при деплое).
    Если файл не найден — скачивает актуальную версию с GitVerse.
    Возвращает список пар на указанную дату, отсортированных по времени.
    """
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
        # dt может быть datetime или date — обрабатываем оба варианта
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
    """Форматирует список пар дня в HTML для отправки в канал."""
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
    """
    Cron-задача в 08:00 МСК — отправляет расписание на сегодня в канал @gasu4ka.
    Если пар нет (выходной, каникулы) — ничего не отправляем, не спамим.
    TG_CHANNEL должен быть задан в переменных окружения Amvera.
    """
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
        return
    await app.bot.send_message(
        chat_id=TG_CHANNEL,
        text=_format_day_schedule(today, lessons),
        parse_mode="HTML",
    )
    log.info("Утреннее расписание отправлено: %d пар", len(lessons))


async def send_lesson_reminder(app, lesson: dict):
    """Отправляет напоминание за 30 мин до пары в канал."""
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


async def schedule_daily_reminders(app):
    """
    Cron-задача в 07:50 МСК (и при старте бота) — планирует APScheduler-джобы
    типа 'date' за 30 мин до каждой пары сегодняшнего дня.
    replace_existing=True — безопасно при повторном вызове (напр. после рестарта).
    Пары у которых remind_dt уже в прошлом — пропускаем.
    """
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
    if scheduled:
        log.info("Запланировано напоминаний: %d", scheduled)


# ─── Настройки ───────────────────────────────────────────────────────────────

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Открывается по кнопке «⚙️ Настройки» из reply_keyboard.
    Показывает inline-меню с двумя действиями:
      - Подключить / Отключить Яндекс.Календарь (кнопка меняется в зависимости от статуса)
      - Удалить аккаунт (спрятано сюда, чтобы не торчало на главном экране)

    «Отключить Яндекс» — удаляет Calendar «СПбГАСУ» из Яндекса и очищает данные в БД.
    «Удалить аккаунт» — ведёт к confirm_unregister, который тоже удалит Calendar.
    """
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user or not user["approved"] or user.get("banned"):
        return
    if user["yandex_login"]:
        yc_btn    = InlineKeyboardButton("📅 Отключить Яндекс.Календарь", callback_data="disconnect_yc")
        yc_status = f"подключён ({user['yandex_login']})"
    else:
        yc_btn    = InlineKeyboardButton("📅 Подключить Яндекс.Календарь", callback_data="connect_yandex")
        yc_status = "не подключён"
    kb = InlineKeyboardMarkup([
        [yc_btn],
        [InlineKeyboardButton("❌ Удалить аккаунт", callback_data="unregister")],
    ])
    await update.message.reply_text(
        f"⚙️ Настройки\n\nЯндекс.Календарь: {yc_status}",
        reply_markup=kb,
    )


# ─── Удаление аккаунта ────────────────────────────────────────────────────────

async def cmd_unregister(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Шаг 1 удаления — показывает подтверждение с предупреждением про календарь.
    Само удаление происходит в on_callback() при confirm_unregister.
    Вызывается из настроек (callback "unregister") или командой /unregister.
    """
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
    """Извлекает telegram_id из строки вида 'action:TID'. Возвращает None при ошибке."""
    try:
        return int(data.split(":")[1])
    except (IndexError, ValueError):
        return None


# ─── Глобальный обработчик inline-кнопок ─────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Ловит ВСЕ inline callback_query, которые не перехватил ConversationHandler.
    ConversationHandler-ы добавлены в app раньше — они имеют приоритет.
    Поэтому сюда попадают только callback-данные вне активных диалогов.

    Структура callback_data:
      - Простые строки: "stats", "unregister", "confirm_unregister", "cancel_action",
                        "disconnect_yc"
      - С параметром через «:»: "approve:TID", "deny:TID", "ban:TID", "unban:TID",
                                "owner_remove:TID"  (TID = telegram_id пользователя)
    """
    query = update.callback_query
    data  = query.data

    if data == "stats":
        await query.answer()
        await cmd_stats(update, ctx)
        return

    if data == "unregister":
        # Показываем экран подтверждения (сам cmd_unregister это делает)
        await query.answer()
        await query.message.reply_text(
            "⚠️ Удалить аккаунт? Все данные (логин, пароль, Яндекс) будут удалены.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Да, удалить", callback_data="confirm_unregister"),
                InlineKeyboardButton("↩️ Отмена",      callback_data="cancel_action"),
            ]])
        )
        return

    if data == "confirm_unregister":
        """
        Финальное удаление аккаунта пользователем.
        Порядок важен: сначала читаем user (нужны данные ЯК), потом удаляем из БД.
        Если удалить сначала — данные ЯК будут уже недоступны.
        show_alert=True — всплывающий попап поверх чата, подтверждает действие.
        """
        tid  = query.from_user.id
        user = get_user(tid)
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
        """
        Отключение Яндекс.Календаря без удаления аккаунта.
        Удаляет сам календарь «СПбГАСУ» из Яндекса, затем очищает данные в БД.
        clear_yandex() ставит yandex_login=NULL и yandex_pass_enc=NULL.
        """
        await query.answer()
        tid  = query.from_user.id
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
    if query.from_user.id != OWNER_ID:
        return

    if data.startswith("owner_remove:"):
        """
        Администратор удаляет пользователя через кнопку в /users.
        Так же как и при самоудалении — сначала удаляем ЯК, потом из БД.
        try/except вокруг send_message — пользователь мог заблокировать бота.
        """
        tid = _tid_from_data(data)
        if tid is None:
            return
        user = get_user(tid)
        if user and user.get("yandex_login") and user.get("yandex_pass"):
            try:
                from sync_yandex import delete_yandex_calendar
                await asyncio.to_thread(
                    delete_yandex_calendar, user["yandex_login"], user["yandex_pass"]
                )
            except Exception as e:
                log.warning("calendar delete error for %s: %s", tid, e)
        remove_user(tid)
        await query.edit_message_text(query.message.text + "\n\n🗑 Удалён")
        try:
            await ctx.bot.send_message(chat_id=tid, text="Твой аккаунт удалён администратором.")
        except Exception:
            pass
        return

    if data.startswith("approve:"):
        tid = _tid_from_data(data)
        if tid is None:
            return
        approve_user(tid)
        await query.edit_message_text(query.message.text + "\n\n✅ Одобрено")
        try:
            await ctx.bot.send_message(
                chat_id=tid,
                text="✅ Твоя заявка одобрена! Можешь пользоваться ботом.",
                reply_markup=reply_keyboard(tid == OWNER_ID),
            )
        except Exception:
            pass

    elif data.startswith("deny:"):
        tid = _tid_from_data(data)
        if tid is None:
            return
        remove_user(tid)
        await query.edit_message_text(query.message.text + "\n\n❌ Отклонено и удалено")
        try:
            await ctx.bot.send_message(chat_id=tid, text="❌ Твоя заявка отклонена.")
        except Exception:
            pass

    elif data.startswith("ban:"):
        tid = _tid_from_data(data)
        if tid is None:
            return
        ban_user(tid)
        await query.edit_message_text(query.message.text + "\n\n🚫 Заблокирован")
        try:
            await ctx.bot.send_message(chat_id=tid, text="🚫 Твой доступ заблокирован.")
        except Exception:
            pass

    elif data.startswith("unban:"):
        tid = _tid_from_data(data)
        if tid is None:
            return
        unban_user(tid)
        await query.edit_message_text(query.message.text + "\n\n✅ Разблокирован")


# ─── Админ-команды (текстовые, дублируют inline-кнопки из /users) ─────────────

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    approve_user(tid)
    await update.message.reply_text(f"✅ {tid} одобрен.")
    try:
        await ctx.bot.send_message(chat_id=tid, text="✅ Твоя заявка одобрена! Напиши /start.")
    except Exception:
        pass


async def cmd_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    remove_user(tid)
    await update.message.reply_text(f"❌ {tid} отклонён.")
    try:
        await ctx.bot.send_message(chat_id=tid, text="❌ Твоя заявка отклонена.")
    except Exception:
        pass


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    ban_user(tid)
    await update.message.reply_text(f"🚫 {tid} заблокирован.")
    try:
        await ctx.bot.send_message(chat_id=tid, text="🚫 Твой доступ заблокирован.")
    except Exception:
        pass


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    unban_user(tid)
    await update.message.reply_text(f"✅ {tid} разблокирован.")


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Список всех пользователей — по одному сообщению на каждого с inline-кнопками.
    toggle_btn меняется в зависимости от статуса:
      активен → кнопка «Заблокировать»
      заблокирован → кнопка «Разбанить»
      ожидает → кнопка «Одобрить»
    """
    if update.effective_user.id != OWNER_ID:
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет пользователей.")
        return
    await update.message.reply_text(f"👥 Пользователей: {len(users)}")
    for u in users:
        tid = u["telegram_id"]
        if u.get("banned"):
            status     = "🚫 Заблокирован"
            toggle_btn = InlineKeyboardButton("✅ Разбанить",       callback_data=f"unban:{tid}")
        elif u["approved"]:
            status     = "✅ Активен"
            toggle_btn = InlineKeyboardButton("🚫 Заблокировать",   callback_data=f"ban:{tid}")
        else:
            status     = "⏳ Ожидает подтверждения"
            toggle_btn = InlineKeyboardButton("✅ Одобрить",         callback_data=f"approve:{tid}")
        yc   = "📅 Яндекс подключён" if u["yandex_login"] else "без Яндекса"
        text = f"{status}\nID: {tid} | Портал: {u['login']}\n{yc}"
        kb   = InlineKeyboardMarkup([[
            toggle_btn,
            InlineKeyboardButton("🗑 Удалить", callback_data=f"owner_remove:{tid}"),
        ]])
        await update.message.reply_text(text, reply_markup=kb)


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Текстовая команда удаления — дублирует кнопку «🗑 Удалить» в /users."""
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи числовой ID.")
        return
    remove_user(tid)
    await update.message.reply_text("✅ Удалён.")


async def cmd_sendhtml(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет debug_lk.html — последний сохранённый HTML страницы /lk/ портала.
    Нужен для отладки парсера когда аттестации не парсятся.
    Файл сохраняется в DATA_DIR (/data на Amvera) при каждом вызове /stats.
    """
    if update.effective_user.id != OWNER_ID:
        return
    from pathlib import Path
    owner = get_user(OWNER_ID)
    login = owner["login"] if owner else "unknown"
    path = Path(os.environ.get("DATA_DIR", ".")) / f"debug_lk_{login}.html"
    if not path.exists():
        await update.message.reply_text(f"{path.name} не найден — сначала вызови /stats")
        return
    await update.message.reply_document(document=open(path, "rb"), filename=path.name)


async def cmd_sendpng(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет debug_lk.png — полностраничный скриншот /lk/ портала.
    Помогает понять что именно видит Playwright: дашборд или форму логина.
    Если на скрине форма логина — значит сессия протухла или логин/пароль неверный.
    """
    if update.effective_user.id != OWNER_ID:
        return
    from pathlib import Path
    owner = get_user(OWNER_ID)
    login = owner["login"] if owner else "unknown"
    path = Path(os.environ.get("DATA_DIR", ".")) / f"debug_lk_{login}.png"
    if not path.exists():
        await update.message.reply_text(f"{path.name} не найден — сначала вызови /stats")
        return
    await update.message.reply_photo(photo=open(path, "rb"))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).post_init(_post_init).post_shutdown(_post_shutdown).build()

    # not_kb — фильтр, исключающий тексты кнопок reply_keyboard из ConversationHandler.
    # Без него нажатие «📋 Аттестации» во время диалога регистрации интерпретировалось бы
    # как ввод логина/пароля — пользователь ломал бы сессию случайным нажатием кнопки.
    not_kb = ~filters.Regex("^(" + "|".join(re.escape(b) for b in KB_BTNS) + ")$")

    # reg_handler — диалог регистрации нового пользователя.
    # allow_reentry=True — пользователь может начать /start заново если передумал в середине.
    # Два entry_point: /start (команда) и callback "register" (inline-кнопка).
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

    # yc_handler — отдельный диалог подключения ЯК уже зарегистрированным пользователем.
    # Отдельный от reg_handler чтобы не смешивать состояния (у них разные state-константы).
    # Entry point — callback "connect_yandex" из кнопки настроек или /connect_yandex.
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

    # ПОРЯДОК ВАЖЕН: ConversationHandler-ы первыми — они перехватывают апдейты раньше.
    # on_callback идёт после — ловит только те callback, что не забрал ни один диалог.
    app.add_handler(reg_handler)
    app.add_handler(yc_handler)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("unregister", cmd_unregister))
    app.add_handler(CommandHandler("approve",    cmd_approve))
    app.add_handler(CommandHandler("deny",       cmd_deny))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CommandHandler("users",      cmd_users))
    app.add_handler(CommandHandler("remove",     cmd_remove))
    app.add_handler(CommandHandler("sendhtml",   cmd_sendhtml))
    app.add_handler(CommandHandler("sendpng",    cmd_sendpng))

    # Обработчики кнопок reply-клавиатуры — добавляются ПОСЛЕ ConversationHandler-ов.
    # Сами тексты кнопок исключены из состояний диалогов через фильтр not_kb выше,
    # поэтому здесь они гарантированно обрабатываются глобальными хендлерами.
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_STATS)}$")    & filters.ChatType.PRIVATE, cmd_stats))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_USERS)}$")    & filters.ChatType.PRIVATE, cmd_users))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_SETTINGS)}$") & filters.ChatType.PRIVATE, cmd_settings))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
