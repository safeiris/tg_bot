"""User interaction handlers for the psychology webinar bot."""
from __future__ import annotations

import re

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database
from config import is_admin, load_settings
from message_templates import (
    build_free_confirmation,
    build_paid_pending_confirmation,
)

WAITING_EMAIL, WAITING_FORMAT = range(2)
FREE_BUTTON = "🆓 Наблюдатель (бесплатно)"
PAID_BUTTON = "💰 Участник с разбором (платно)"

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user and is_admin(chat_id=user.id, username=user.username):
        from admin_panel import send_admin_panel

        await send_admin_panel(update, context)
        return ConversationHandler.END

    await update.message.reply_text(
        "Добро пожаловать на вебинар по психологии!\n"
        "Пожалуйста, укажите ваш e-mail для регистрации.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAITING_EMAIL


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = (update.message.text or "").strip()
    if not EMAIL_REGEX.match(email):
        await update.message.reply_text("Похоже, это не e-mail. Попробуйте снова.")
        return WAITING_EMAIL

    user = update.effective_user
    participant = database.Participant(
        name=(user.full_name or "") if user else "",
        username=f"@{user.username}" if user and user.username else "",
        chat_id=update.effective_chat.id,
        email=email,
    )
    try:
        database.register_participant(participant)
    except RuntimeError:
        await update.message.reply_text(
            "Регистрация временно недоступна. Пожалуйста, попробуйте позже.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Вы зарегистрированы на вебинар 💫 Ссылка придёт в день проведения.",
    )

    keyboard = ReplyKeyboardMarkup([[FREE_BUTTON], [PAID_BUTTON]], resize_keyboard=True)
    await update.message.reply_text("Выберите формат участия:", reply_markup=keyboard)
    return WAITING_FORMAT


async def handle_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = (update.message.text or "").strip()
    settings = load_settings()
    if choice == FREE_BUTTON:
        database.update_participation(update.effective_chat.id, "free", "no")
        confirmation = build_free_confirmation(settings)
        await update.message.reply_text(confirmation, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if choice == PAID_BUTTON:
        database.update_participation(update.effective_chat.id, "paid", "no")
        message = build_paid_pending_confirmation(settings)
        await update.message.reply_text(message, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    await update.message.reply_text(
        "Пожалуйста, используйте кнопки для выбора формата участия."
    )
    return WAITING_FORMAT


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Регистрация отменена.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    awaiting = context.application.bot_data.setdefault("awaiting_feedback", set())
    if chat_id not in awaiting:
        return
    feedback = (update.message.text or "").strip()
    if not feedback:
        return
    database.update_feedback(chat_id, feedback)
    awaiting.discard(chat_id)
    await update.message.reply_text("Спасибо за обратную связь! 💖")


def build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)],
            WAITING_FORMAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_format)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
