"""
Telegram Bot entry point for the Pixel 10 Pro Google One Gemini Bot.

Commands:
  /start        – Show welcome message and available commands
  /login        – Begin credential capture flow (email → password)
  /check_offer  – Run Google One automation and look for Gemini Pro offer
  /get_link     – Show the last captured offer link
  /status       – Show current session status and device profile
"""

import logging
import os
import sys

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
from device_simulator import create_device_profile
from google_automation import (
    GoogleAutomationError,
    check_gemini_offer,
    initiate_login,
    complete_login_and_check_offer,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
AWAIT_EMAIL, AWAIT_PASSWORD, AWAIT_2FA_CODE = range(3)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_session(chat_id: int) -> dict:
    """Return (creating if absent) the session dict for *chat_id*."""
    if chat_id not in config.SESSION_STORE:
        config.SESSION_STORE[chat_id] = {}
    return config.SESSION_STORE[chat_id]


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with command menu."""
    await update.message.reply_text(
        "🤖 *Pixel 10 Pro Google One Bot*\n\n"
        "This bot simulates a Google Pixel 10 Pro (Android 16) device, "
        "logs into your Google account, and retrieves the *12-month free "
        "Gemini Pro* offer link from Google One.\n\n"
        "📋 *Available Commands:*\n"
        "• /login – Enter your Gmail credentials\n"
        "• /check\\_offer – Detect the Gemini Pro offer\n"
        "• /get\\_link – Show the last captured offer link\n"
        "• /status – View current session & device info\n\n"
        "⚠️ *Privacy Note:* Credentials are held in memory only for the "
        "duration of the session and never stored persistently.",
        parse_mode="Markdown",
    )


# ── /login conversation ───────────────────────────────────────────────────────

async def login_start(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the login conversation – ask for email."""
    await update.message.reply_text(
        "📧 Please enter your Gmail address:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AWAIT_EMAIL


async def login_email(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the email and ask for password."""
    email = update.message.text.strip()
    context.user_data["pending_email"] = email
    await update.message.reply_text(
        f"✅ Email received: `{email}`\n\n🔒 Now enter your password:",
        parse_mode="Markdown",
    )
    return AWAIT_PASSWORD


async def login_password(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store credentials, generate a new device profile, and finish."""
    chat_id = update.effective_chat.id
    password = update.message.text.strip()
    email = context.user_data.pop("pending_email", "")

    session = _get_session(chat_id)
    session["email"] = email
    session["password"] = password
    session["device"] = create_device_profile()
    session["offer_link"] = None

    # Delete the message containing the password for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ *Credentials saved* and a new Pixel 10 Pro device profile has "
            "been created for this session.\n\n"
            + session["device"].summary()
            + "\n\nUse /check\\_offer to search for the Gemini Pro offer."
        ),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def login_cancel(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the login conversation."""
    context.user_data.pop("pending_email", None)
    await update.message.reply_text(
        "❌ Login cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /check_offer ──────────────────────────────────────────────────────────────

async def check_offer_start(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> int:
    """Run Google One automation. Returns next state if 2FA is needed."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session.get("email") or not session.get("password"):
        await update.message.reply_text(
            "⚠️ No credentials found. Please use /login first."
        )
        return ConversationHandler.END

    device = session.get("device")
    if not device:
        device = create_device_profile()
        session["device"] = device

    await update.message.reply_text(
        "⏳ Launching Pixel 10 Pro device simulator and logging in…\n"
        "This may take up to 60 seconds."
    )

    try:
        browser, context, page, status = initiate_login(
            session["email"],
            session["password"],
            device,
        )
    except GoogleAutomationError as exc:
        await update.message.reply_text(f"❌ *Error:* {exc}", parse_mode="Markdown")
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("Unexpected error in check_offer for chat %s", chat_id)
        await update.message.reply_text(
            f"❌ An unexpected error occurred: {exc}"
        )
        return ConversationHandler.END

    if status == "2fa":
        session["pending_browser"] = browser
        session["pending_page"] = page
        await update.message.reply_text(
            "🔐 *检测到两步验证*\n\n"
            "请输入你的 Google 身份验证器中的 6 位验证码：",
            parse_mode="Markdown",
        )
        return AWAIT_2FA_CODE

    if not status:
        browser.close()
        await update.message.reply_text(
            "❌ *Error:* Login failed – please check your credentials.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Login succeeded directly – navigate to Google One
    try:
        offer_link = complete_login_and_check_offer(page)
    except Exception:
        offer_link = None
    finally:
        browser.close()

    if offer_link:
        session["offer_link"] = offer_link
        await update.message.reply_text(
            "🎉 *Gemini Pro Offer Found!*\n\n"
            "Click the link below to activate your 12-month free Gemini Pro:\n\n"
            f"🔗 {offer_link}\n\n"
            "_Use /get\\_link to retrieve this link again._",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "😔 No active Gemini Pro offer was detected on your Google One "
            "account at this time.\n\n"
            "The offer may not be available for your account region or may "
            "have already been activated. Try again later."
        )

    return ConversationHandler.END


async def two_fa_code_input(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle 2FA verification code input from the user."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    code = update.message.text.strip()

    # Delete the code message for security
    try:
        await update.message.delete()
    except Exception:
        pass

    # Validate code format
    if not code.isdigit() or len(code) < 4:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ 验证码格式不正确，请输入 6 位数字验证码：",
        )
        return AWAIT_2FA_CODE

    driver = session.pop("pending_driver", None)
    browser = session.pop("pending_browser", None)
    page = session.pop("pending_page", None)
    if not browser or not page:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ 会话已过期，请重新 /check\_offer",
        )
        return ConversationHandler.END

    await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ 正在验证…",
    )

    login_ok = True
    try:
        offer_link = complete_login_and_check_offer(page, two_fa_code=code)
        if offer_link is None:
            login_ok = _still_logged_in(page)
    except Exception as exc:
        logger.exception("Error during 2FA completion for chat %s", chat_id)
        offer_link = None
        login_ok = False
    finally:
        try:
            browser.close()
        except Exception:
            pass

    if offer_link is None and not login_ok:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ 验证码错误或已过期，请重新 /check\_offer",
        )
        return ConversationHandler.END

    if offer_link:
        session["offer_link"] = offer_link
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🎉 *Gemini Pro Offer Found!*\n\n"
                "Click the link below to activate your 12-month free Gemini Pro:\n\n"
                f"🔗 {offer_link}\n\n"
                "_Use /get\\_link to retrieve this link again._"
            ),
            parse_mode="Markdown",
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ 登录成功，但未检测到 Gemini Pro 优惠。\n\n"
                "该优惠可能不适用于你的账户地区，或已被激活。请稍后再试。"
            ),
        )

    return ConversationHandler.END


def _still_logged_in(page) -> bool:
    """Quick check if the page is still on a logged-in Google page."""
    try:
        current_url = page.url
        return "myaccount.google.com" in current_url or "/u/" in current_url
    except Exception:
        return False


async def two_fa_cancel(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the 2FA flow and clean up the browser."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    browser = session.pop("pending_browser", None)
    session.pop("pending_page", None)
    if browser:
        try:
            browser.close()
        except Exception:
            pass

    await update.message.reply_text("❌ 已取消。")
    return ConversationHandler.END


# ── /get_link ─────────────────────────────────────────────────────────────────

async def get_link(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the last captured offer link for this session."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    link = session.get("offer_link")

    if link:
        await update.message.reply_text(
            f"🔗 *Last captured offer link:*\n\n{link}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "ℹ️ No offer link has been captured yet. "
            "Use /check\\_offer to search for the Gemini Pro offer.",
            parse_mode="Markdown",
        )


# ── /status ───────────────────────────────────────────────────────────────────

async def status(update: Update,
                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session and device profile summary."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session:
        await update.message.reply_text(
            "ℹ️ No active session. Use /login to get started."
        )
        return

    email = session.get("email", "—")
    has_creds = bool(session.get("email") and session.get("password"))
    offer_link = session.get("offer_link")
    device = session.get("device")

    lines = [
        "📊 *Session Status*\n",
        f"Account: `{email}`",
        f"Credentials loaded: {'✅' if has_creds else '❌'}",
        f"Offer link captured: {'✅' if offer_link else '❌'}",
    ]

    if device:
        lines.append("\n" + device.summary())

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ── Application setup ─────────────────────────────────────────────────────────

def main() -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Set it in Replit Secrets and restart."
        )
        sys.exit(1)

    app = Application.builder().token(token).build()

    # /login conversation
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            AWAIT_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_email)
            ],
            AWAIT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)
            ],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(login_conv)
    # /check_offer conversation (handles 2FA flow)
    check_offer_conv = ConversationHandler(
        entry_points=[CommandHandler("check_offer", check_offer_start)],
        states={
            AWAIT_2FA_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, two_fa_code_input)
            ],
        },
        fallbacks=[CommandHandler("cancel", two_fa_cancel)],
    )
    app.add_handler(check_offer_conv)
    app.add_handler(CommandHandler("get_link", get_link))
    app.add_handler(CommandHandler("status", status))

    logger.info("Bot is running. Press Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
