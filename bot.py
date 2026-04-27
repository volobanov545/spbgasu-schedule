#!/usr/bin/env python3
import asyncio
import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from db import init_db, add_user, set_yandex, approve_user, remove_user, get_user, get_all_users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN    = os.environ["TG_TOKEN"]
OWNER_ID = int(os.environ["TG_OWNER_ID"])

WAIT_LOGIN, WAIT_PASSWORD, WAIT_YC_CHOICE, WAIT_YC_LOGIN, WAIT_YC_PASS = range(5)

YC_INSTRUCTION = (
    "📅 Чтобы подключить Яндекс.Календарь:\n\n"
    "1. Открой id.yandex.ru\n"
    "2. Безопасность → Пароли приложений\n"
    "3. Нажми «Создать пароль» → выбери «Другое»\n"
    "4. Скопируй пароль из 16 символов\n\n"
    "Введи свой логин Яндекса (без @yandex.ru):"
)


# ─── Регистрация ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if user:
        yc = "подключён ✅" if user["yandex_login"] else "не подключён — /connect_yandex"
        await update.message.reply_text(
            f"Ты уже зарегистрирован (логин: {user['login']}).\n"
            f"Яндекс.Календарь: {yc}\n\n"
            "/stats — статистика\n"
            "/unregister — удалить аккаунт"
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
    answer = update.message.text.strip().lower()
    if answer in ("да", "yes", "y", "д"):
        await update.message.reply_text(YC_INSTRUCTION)
        return WAIT_YC_LOGIN
    return await _finish_registration(update, ctx)


async def got_yc_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["yc_login"] = update.message.text.strip()
    await update.message.reply_text("Теперь введи пароль приложения (16 символов):")
    return WAIT_YC_PASS


async def got_yc_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["yc_pass"] = update.message.text.strip()
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

    log.info("Pending approval: user %s login=%s yandex=%s", tid, login, bool(yc_login))
    await update.message.reply_text(
        "✅ Данные получены! Ожидай подтверждения от администратора.\n"
        "Как только тебя одобрят — сможешь пользоваться /stats."
    )
    yc_status = f"Яндекс: {yc_login}" if yc_login else "Яндекс: не подключён"
    await ctx.bot.send_message(
        chat_id=OWNER_ID,
        text=(
            f"🔔 Новая заявка на регистрацию:\n"
            f"Пользователь: @{username} (id: {tid})\n"
            f"Логин портала: {login}\n"
            f"{yc_status}\n\n"
            f"/approve {tid} — одобрить\n"
            f"/deny {tid} — отклонить"
        ),
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def cmd_unregister(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    remove_user(update.effective_user.id)
    await update.message.reply_text("Аккаунт удалён.")


# ─── Яндекс.Календарь (добавить позже) ───────────────────────────────────────

WAIT_YC2_LOGIN, WAIT_YC2_PASS = range(10, 12)


async def cmd_connect_yandex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Сначала зарегистрируйся через /start.")
        return ConversationHandler.END
    await update.message.reply_text(YC_INSTRUCTION)
    return WAIT_YC2_LOGIN


async def got_yc2_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["yc_login"] = update.message.text.strip()
    await update.message.reply_text("Теперь введи пароль приложения:")
    return WAIT_YC2_PASS


async def got_yc2_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yc_login = ctx.user_data.pop("yc_login", "")
    yc_pass  = update.message.text.strip()
    set_yandex(update.effective_user.id, yc_login, yc_pass)
    await update.message.reply_text("✅ Яндекс.Календарь подключён!")
    return ConversationHandler.END


# ─── Статистика ───────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Ты не зарегистрирован. Напиши /start.")
        return
    if not user["approved"]:
        await update.message.reply_text("⏳ Твоя заявка ещё не подтверждена администратором.")
        return
    await update.message.reply_text("⏳ Загружаю данные с портала, подожди ~30 сек...")
    try:
        from parse_journals import parse_lk_quick
        data = await asyncio.to_thread(parse_lk_quick, user["login"], user["password"])
        text = _format_stats(data)
    except Exception as e:
        log.exception("stats error for %s", user["login"])
        await update.message.reply_text(f"❌ Ошибка при загрузке: {e}")
        return
    await update.message.reply_text(text)

    # Синхронизация Яндекс.Календаря если подключён
    if user["yandex_login"] and user["yandex_pass"]:
        try:
            from sync_yandex import sync_calendar
            from pathlib import Path
            ics = Path(__file__).parent / "schedule.ics"
            synced = await asyncio.to_thread(
                sync_calendar, user["yandex_login"], user["yandex_pass"], ics
            )
            await update.message.reply_text(f"📅 Яндекс.Календарь обновлён ({synced} событий).")
        except Exception as e:
            log.warning("yandex sync error: %s", e)
            await update.message.reply_text(f"⚠️ Яндекс.Календарь: ошибка синхронизации — {e}")


def _format_stats(data: dict) -> str:
    lines = ["📋 Аттестации:\n"]
    atts = data.get("attestations", {})
    for subj, marks in atts.items():
        a1 = marks.get("att1", "—")
        a2 = marks.get("att2", "—")
        lines.append(f"  {subj}: 1-я {a1} / 2-я {a2}")
    return "\n".join(lines)


# ─── Админ-команды ────────────────────────────────────────────────────────────

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /approve <telegram_id>")
        return
    tid = int(ctx.args[0])
    approve_user(tid)
    await update.message.reply_text(f"✅ Пользователь {tid} одобрен.")
    try:
        await ctx.bot.send_message(chat_id=tid, text="✅ Твоя заявка одобрена! Теперь можешь использовать /stats.")
    except Exception:
        pass


async def cmd_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /deny <telegram_id>")
        return
    tid = int(ctx.args[0])
    remove_user(tid)
    await update.message.reply_text(f"❌ Пользователь {tid} отклонён и удалён.")
    try:
        await ctx.bot.send_message(chat_id=tid, text="❌ Твоя заявка отклонена.")
    except Exception:
        pass


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет зарегистрированных пользователей.")
        return
    lines = [f"👥 Пользователи ({len(users)}):"]
    for u in users:
        status = "✅" if u["approved"] else "⏳"
        yc     = " 📅" if u["yandex_login"] else ""
        lines.append(f"  {status}{yc} {u['telegram_id']} — {u['login']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /remove <telegram_id>")
        return
    tid = int(ctx.args[0])
    remove_user(tid)
    await update.message.reply_text(f"✅ Пользователь {tid} удалён.")


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
    )

    yc_handler = ConversationHandler(
        entry_points=[CommandHandler("connect_yandex", cmd_connect_yandex, filters=filters.ChatType.PRIVATE)],
        states={
            WAIT_YC2_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_yc2_login)],
            WAIT_YC2_PASS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_yc2_pass)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(reg_handler)
    app.add_handler(yc_handler)
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("unregister", cmd_unregister))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("remove", cmd_remove))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
