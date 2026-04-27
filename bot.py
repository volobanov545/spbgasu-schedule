#!/usr/bin/env python3
import asyncio
import logging
import os
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
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

from db import init_db, add_user, set_yandex, approve_user, ban_user, unban_user, remove_user, get_user, get_all_users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN    = os.environ["TG_TOKEN"]
OWNER_ID = int(os.environ["TG_OWNER_ID"])

WAIT_LOGIN, WAIT_PASSWORD, WAIT_YC_CHOICE, WAIT_YC_LOGIN, WAIT_YC_PASS = range(5)
WAIT_YC2_LOGIN, WAIT_YC2_PASS = range(10, 12)

def _test_yandex(ylogin: str, ypass: str) -> str | None:
    """Возвращает None если OK, иначе текст ошибки."""
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


BTN_STATS  = "📋 Аттестации"
BTN_YC     = "📅 Яндекс.Календарь"
BTN_USERS  = "👥 Пользователи"
BTN_DELETE = "❌ Удалить аккаунт"
KB_BTNS = {BTN_STATS, BTN_YC, BTN_USERS, BTN_DELETE}


def reply_keyboard(is_owner: bool = False, has_yc: bool = False) -> ReplyKeyboardMarkup:
    top = [KeyboardButton(BTN_STATS)] if has_yc else [KeyboardButton(BTN_STATS), KeyboardButton(BTN_YC)]
    rows = [top]
    if is_owner:
        rows.append([KeyboardButton(BTN_USERS)])
    rows.append([KeyboardButton(BTN_DELETE)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Аттестации", callback_data="stats")],
        [InlineKeyboardButton("📅 Яндекс.Календарь", callback_data="connect_yandex")],
        [InlineKeyboardButton("❌ Удалить аккаунт", callback_data="unregister")],
    ])


# ─── Регистрация ──────────────────────────────────────────────────────────────

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
                f"⏳ Ты уже зарегистрирован (логин: {user['login']}).\n"
                "Заявка ещё не подтверждена администратором — ожидай."
            )
            return ConversationHandler.END
        yc = "подключён ✅" if user["yandex_login"] else "не подключён"
        await update.message.reply_text(
            f"Привет! Ты зарегистрирован (логин: {user['login']}).\n"
            f"Яндекс.Календарь: {yc}",
            reply_markup=reply_keyboard(update.effective_user.id == OWNER_ID, has_yc=bool(user["yandex_login"])),
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "Привет! Я слежу за расписанием и журналами СПбГАСУ.\n\n"
        "Введи свой логин от портала:"
    )
    return WAIT_LOGIN


async def got_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["login"] = update.message.text.strip()
    await update.message.reply_text("Теперь введи пароль:")
    return WAIT_PASSWORD


async def got_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["password"] = update.message.text.strip()
    await update.message.reply_text(
        "Хочешь подключить Яндекс.Календарь?\n"
        "Расписание будет автоматически появляться в твоём календаре.\n\n"
        "Напиши «да» или «нет»:"
    )
    return WAIT_YC_CHOICE


async def got_yc_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() in ("да", "yes", "y", "д"):
        await update.message.reply_text(YC_INSTRUCTION)
        return WAIT_YC_LOGIN
    return await _finish_registration(update, ctx)


async def got_yc_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["yc_login"] = update.message.text.strip()
    await update.message.reply_text("Теперь введи пароль приложения (16 символов):")
    return WAIT_YC_PASS


async def got_yc_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yc_login = ctx.user_data.get("yc_login", "")
    yc_pass  = update.message.text.strip()
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
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{tid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"deny:{tid}"),
        ]
    ])
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


# ─── Яндекс.Календарь ────────────────────────────────────────────────────────

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
    ctx.user_data["yc_login"] = update.message.text.strip()
    await update.message.reply_text("Теперь введи пароль приложения:")
    return WAIT_YC2_PASS


async def got_yc2_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yc_login = ctx.user_data.get("yc_login", "")
    yc_pass  = update.message.text.strip()
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
        reply_markup=reply_keyboard(update.effective_user.id == OWNER_ID, has_yc=True),
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

    await send("⏳ Загружаю данные с портала, подожди ~20 сек...")
    try:
        from parse_journals import parse_lk_quick
        data = await asyncio.to_thread(parse_lk_quick, user["login"], user["password"])
        text = _format_stats(data)
    except Exception as e:
        log.exception("stats error for %s", user["login"])
        await send(f"❌ Ошибка при загрузке: {e}")
        return
    await send(text, reply_markup=main_menu())

    if user["yandex_login"] and user["yandex_pass"]:
        try:
            from sync_yandex import sync_calendar
            from pathlib import Path
            ics = Path(__file__).parent / "schedule.ics"
            await asyncio.to_thread(sync_calendar, user["yandex_login"], user["yandex_pass"], ics)
        except Exception as e:
            log.warning("yandex sync error: %s", e)
            await send("⚠️ Яндекс.Календарь: ошибка авторизации.\nПроверь пароль приложения — нажми «📅 Яндекс.Календарь» и введи заново.")


def _format_stats(data: dict) -> str:
    lines = []

    stats = data.get("stats", {})
    if stats:
        total   = stats.get("total_classes", "?")
        present = stats.get("present_pct", "?")
        absent  = stats.get("absent_pct", "?")
        lines.append(f"📊 Посещаемость: {present}% присутствий, {absent}% пропусков ({total} занятий)")

    attestations = data.get("attestations", {})
    if attestations:
        lines.append("\n📋 Аттестации:")
        for subj, marks in attestations.items():
            a1 = marks.get("att1") or "—"
            a2 = marks.get("att2") or "—"
            lines.append(f"  {subj}: 1-я {a1} / 2-я {a2}")
    else:
        lines.append("\n📋 Аттестации: данных нет (семестр ещё не начался или страница изменилась)")

    return "\n".join(lines) if lines else "Нет данных."


# ─── Удаление аккаунта ────────────────────────────────────────────────────────

async def cmd_unregister(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        send = query.message.reply_text
        tid = query.from_user.id
    else:
        send = update.message.reply_text
        tid = update.effective_user.id

    user = get_user(tid)
    cal_note = "\n📅 Календарь «СПбГАСУ» в Яндексе будет удалён." if (user and user.get("yandex_login")) else ""
    await send(
        f"⚠️ Удалить аккаунт? Все данные (логин, пароль, Яндекс) будут удалены.{cal_note}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Да, удалить", callback_data="confirm_unregister"),
            InlineKeyboardButton("↩️ Отмена", callback_data="cancel_action"),
        ]]),
    )


# ─── Callback-кнопки (approve / deny / ban) ──────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # Кнопки для всех пользователей
    if data == "stats":
        await query.answer()
        await cmd_stats(update, ctx)
        return

    if data == "unregister":
        await query.answer()
        await query.message.reply_text(
            "⚠️ Удалить аккаунт? Все данные (логин, пароль, Яндекс) будут удалены.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Да, удалить", callback_data="confirm_unregister"),
                InlineKeyboardButton("↩️ Отмена", callback_data="cancel_action"),
            ]])
        )
        return

    if data == "confirm_unregister":
        await query.answer()
        tid = query.from_user.id
        user = get_user(tid)
        cal_msg = ""
        if user and user.get("yandex_login") and user.get("yandex_pass"):
            try:
                from sync_yandex import delete_yandex_calendar
                deleted = await asyncio.to_thread(delete_yandex_calendar, user["yandex_login"], user["yandex_pass"])
                cal_msg = " Календарь удалён ✓" if deleted else ""
            except Exception as e:
                log.warning("calendar delete error: %s", e)
        remove_user(tid)
        await query.edit_message_text(f"Аккаунт удалён.{cal_msg} Напиши /start чтобы зарегистрироваться снова.")
        return

    if data == "cancel_action":
        await query.answer()
        await query.edit_message_text("Отменено.")
        return

    # Админские действия — только для овнера
    await query.answer()
    if query.from_user.id != OWNER_ID:
        return

    if data.startswith("owner_remove:"):
        tid = int(data.split(":")[1])
        user = get_user(tid)
        if user and user.get("yandex_login") and user.get("yandex_pass"):
            try:
                from sync_yandex import delete_yandex_calendar
                await asyncio.to_thread(delete_yandex_calendar, user["yandex_login"], user["yandex_pass"])
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
        tid = int(data.split(":")[1])
        approve_user(tid)
        await query.edit_message_text(query.message.text + "\n\n✅ Одобрено")
        try:
            approved_user = get_user(tid)
            await ctx.bot.send_message(
                chat_id=tid,
                text="✅ Твоя заявка одобрена! Можешь пользоваться ботом.",
                reply_markup=reply_keyboard(tid == OWNER_ID, has_yc=bool(approved_user and approved_user.get("yandex_login"))),
            )
        except Exception:
            pass

    elif data.startswith("deny:"):
        tid = int(data.split(":")[1])
        remove_user(tid)
        await query.edit_message_text(query.message.text + "\n\n❌ Отклонено и удалено")
        try:
            await ctx.bot.send_message(chat_id=tid, text="❌ Твоя заявка отклонена.")
        except Exception:
            pass

    elif data.startswith("ban:"):
        tid = int(data.split(":")[1])
        ban_user(tid)
        await query.edit_message_text(query.message.text + "\n\n🚫 Заблокирован")
        try:
            await ctx.bot.send_message(chat_id=tid, text="🚫 Твой доступ заблокирован.")
        except Exception:
            pass

    elif data.startswith("unban:"):
        tid = int(data.split(":")[1])
        unban_user(tid)
        await query.edit_message_text(query.message.text + "\n\n✅ Разблокирован")


# ─── Админ-команды ────────────────────────────────────────────────────────────

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    tid = int(ctx.args[0])
    approve_user(tid)
    await update.message.reply_text(f"✅ {tid} одобрен.")
    try:
        await ctx.bot.send_message(chat_id=tid, text="✅ Твоя заявка одобрена! Напиши /start.")
    except Exception:
        pass


async def cmd_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    tid = int(ctx.args[0])
    remove_user(tid)
    await update.message.reply_text(f"❌ {tid} отклонён.")
    try:
        await ctx.bot.send_message(chat_id=tid, text="❌ Твоя заявка отклонена.")
    except Exception:
        pass


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    tid = int(ctx.args[0])
    ban_user(tid)
    await update.message.reply_text(f"🚫 {tid} заблокирован.")
    try:
        await ctx.bot.send_message(chat_id=tid, text="🚫 Твой доступ заблокирован.")
    except Exception:
        pass


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    tid = int(ctx.args[0])
    unban_user(tid)
    await update.message.reply_text(f"✅ {tid} разблокирован.")


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
            status = "🚫 Заблокирован"
            toggle_btn = InlineKeyboardButton("✅ Разбанить", callback_data=f"unban:{tid}")
        elif u["approved"]:
            status = "✅ Активен"
            toggle_btn = InlineKeyboardButton("🚫 Заблокировать", callback_data=f"ban:{tid}")
        else:
            status = "⏳ Ожидает подтверждения"
            toggle_btn = InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{tid}")
        yc = "📅 Яндекс подключён" if u["yandex_login"] else "без Яндекса"
        text = f"{status}\nID: {tid} | Портал: {u['login']}\n{yc}"
        kb = InlineKeyboardMarkup([[
            toggle_btn,
            InlineKeyboardButton("🗑 Удалить", callback_data=f"owner_remove:{tid}"),
        ]])
        await update.message.reply_text(text, reply_markup=kb)


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    remove_user(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Удалён.")


async def cmd_sendhtml(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    from pathlib import Path
    path = Path(os.environ.get("DATA_DIR", ".")) / "debug_lk.html"
    if not path.exists():
        await update.message.reply_text("debug_lk.html не найден — сначала вызови /stats")
        return
    await update.message.reply_document(document=open(path, "rb"), filename="debug_lk.html")


async def cmd_sendpng(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    from pathlib import Path
    path = Path(os.environ.get("DATA_DIR", ".")) / "debug_lk.png"
    if not path.exists():
        await update.message.reply_text("debug_lk.png не найден — сначала вызови /stats")
        return
    await update.message.reply_photo(photo=open(path, "rb"))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    not_kb = ~filters.Regex("^(" + "|".join(re.escape(b) for b in KB_BTNS) + ")$")

    reg_handler = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start, filters=filters.ChatType.PRIVATE)],
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
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("unregister", cmd_unregister))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("sendhtml", cmd_sendhtml))
    app.add_handler(CommandHandler("sendpng", cmd_sendpng))

    # Обработчики кнопок reply-клавиатуры
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_STATS)}$")  & filters.ChatType.PRIVATE, cmd_stats))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_YC)}$")     & filters.ChatType.PRIVATE, cmd_connect_yandex))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_USERS)}$")  & filters.ChatType.PRIVATE, cmd_users))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DELETE)}$") & filters.ChatType.PRIVATE, cmd_unregister))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
