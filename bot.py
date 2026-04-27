#!/usr/bin/env python3
import logging
import os
import sys

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from db import init_db, add_user, approve_user, remove_user, get_user, get_all_users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN = os.environ["TG_TOKEN"]
OWNER_ID = int(os.environ["TG_OWNER_ID"])

WAIT_LOGIN, WAIT_PASSWORD = range(2)


# ─── Регистрация ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if user:
        await update.message.reply_text(
            f"Ты уже зарегистрирован (логин: {user['login']}).\n"
            "/stats — твоя статистика\n"
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
    login = ctx.user_data.pop("login", "")
    password = update.message.text.strip()
    tid = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or str(tid)
    add_user(tid, login, password)
    log.info("Pending approval: user %s login=%s", tid, login)
    await update.message.reply_text(
        "✅ Данные получены! Ожидай подтверждения от администратора.\n"
        "Как только тебя одобрят — сможешь пользоваться /stats."
    )
    # Уведомляем владельца
    await ctx.bot.send_message(
        chat_id=OWNER_ID,
        text=(
            f"🔔 Новая заявка на регистрацию:\n"
            f"Пользователь: @{username} (id: {tid})\n"
            f"Логин портала: {login}\n\n"
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


# ─── Статистика ───────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            "Ты не зарегистрирован. Напиши /start чтобы добавить свой аккаунт."
        )
        return
    if not user["approved"]:
        await update.message.reply_text("⏳ Твоя заявка ещё не подтверждена администратором.")
        return
    await update.message.reply_text("⏳ Загружаю данные с портала, подожди ~30 сек...")
    try:
        from parse_journals import parse_lk_main
        data = await ctx.application.loop.run_in_executor(
            None, parse_lk_main, user["login"], user["password"]
        )
        text = _format_stats(data)
    except Exception as e:
        log.exception("stats error for %s", user["login"])
        await update.message.reply_text(f"❌ Ошибка при загрузке: {e}")
        return
    await update.message.reply_text(text)


def _format_stats(data: dict) -> str:
    lines = ["📊 Твоя статистика:\n"]
    stats = data.get("stats", {})
    if stats:
        lines.append(
            f"Занятий проведено: {stats.get('total', '?')}\n"
            f"Присутствовал: {stats.get('present_pct', '?')}%\n"
            f"Отсутствовал: {stats.get('absent_pct', '?')}%\n"
        )
    atts = data.get("attestations", {})
    if atts:
        lines.append("📋 Аттестации:")
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
        lines.append(f"  {status} {u['telegram_id']} — {u['login']}")
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
            WAIT_LOGIN:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_login)],
            WAIT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(reg_handler)
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
