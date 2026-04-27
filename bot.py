#!/usr/bin/env python3
import asyncio
import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
            f"Яндекс.Календарь: {yc}\n\n"
            "Что хочешь сделать?",
            reply_markup=main_menu(),
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
    await update.message.reply_text("✅ Яндекс.Календарь подключён!", reply_markup=main_menu())
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
            synced = await asyncio.to_thread(sync_calendar, user["yandex_login"], user["yandex_pass"], ics)
            await send(f"📅 Яндекс.Календарь обновлён ({synced} событий).")
        except Exception as e:
            log.warning("yandex sync error: %s", e)
            await send(f"⚠️ Яндекс.Календарь: ошибка авторизации.\nПроверь пароль приложения — нажми «📅 Яндекс.Календарь» и введи заново.")


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
        remove_user(query.from_user.id)
        await query.message.reply_text("Аккаунт удалён.")
    else:
        remove_user(update.effective_user.id)
        await update.message.reply_text("Аккаунт удалён.")


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
        remove_user(query.from_user.id)
        await query.edit_message_text("Аккаунт удалён. Напиши /start чтобы зарегистрироваться снова.")
        return

    if data == "cancel_action":
        await query.answer()
        await query.edit_message_text("Отменено.")
        return

    # Админские действия — только для овнера
    await query.answer()
    if query.from_user.id != OWNER_ID:
        return

    if data.startswith("approve:"):
        tid = int(data.split(":")[1])
        approve_user(tid)
        await query.edit_message_text(query.message.text + "\n\n✅ Одобрено")
        try:
            await ctx.bot.send_message(chat_id=tid, text="✅ Твоя заявка одобрена! Напиши /start.")
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
    lines = [f"👥 Пользователи ({len(users)}):"]
    for u in users:
        if u.get("banned"):
            status = "🚫"
        elif u["approved"]:
            status = "✅"
        else:
            status = "⏳"
        yc = " 📅" if u["yandex_login"] else ""
        lines.append(f"  {status}{yc} {u['telegram_id']} — {u['login']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not ctx.args:
        return
    remove_user(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Удалён.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    reg_handler = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start, filters=filters.ChatType.PRIVATE)],
        states={
            WAIT_LOGIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_login)],
            WAIT_PASSWORD:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
            WAIT_YC_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_yc_choice)],
            WAIT_YC_LOGIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_yc_login)],
            WAIT_YC_PASS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_yc_pass)],
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
            WAIT_YC2_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_yc2_login)],
            WAIT_YC2_PASS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_yc2_pass)],
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

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
