"""
Infobip SMS Telegram Bot
========================
A production-ready Telegram bot that sends SMS messages via the Infobip API.

Features:
  • /start  – Welcome message and quick-start guide
  • /help   – Full command reference
  • /sendsms – Step-by-step wizard: collect a +855 phone number and message text
  • /status  – Show bot uptime, your send count, and rate-limit status
  • /cancel  – Abort the current /sendsms wizard at any step

Phone numbers:
  • Strictly +855 (Cambodia) only.
  • Users may enter the number with or without a leading 0 and with or without
    the country code (e.g. 012345678, 12345678, +85512345678 are all accepted).

Rate limiting:
  • Each Telegram user is limited to RATE_LIMIT_MAX_MESSAGES sends within a
    RATE_LIMIT_WINDOW_SECONDS rolling window.

Logging:
  • Logs are written both to stdout and to logs/bot.log (rotating, 5 × 5 MB).

Environment variables (see .env.example):
  TELEGRAM_BOT_TOKEN, INFOBIP_BASE_URL, INFOBIP_API_KEY,
  INFOBIP_SENDER, RATE_LIMIT_MAX_MESSAGES, RATE_LIMIT_WINDOW_SECONDS, LOG_LEVEL
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Deque, Dict, Optional

import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration & constants
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

# Required environment variables
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
INFOBIP_BASE_URL: str = os.environ["INFOBIP_BASE_URL"].rstrip("/")
INFOBIP_API_KEY: str = os.environ["INFOBIP_API_KEY"]

# Optional environment variables with sensible defaults
INFOBIP_SENDER: str = os.getenv("INFOBIP_SENDER", "InfoSMS")
RATE_LIMIT_MAX_MESSAGES: int = int(os.getenv("RATE_LIMIT_MAX_MESSAGES", "5"))
RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Conversation states
STEP_PHONE = 0
STEP_TEXT = 1

# Cambodian phone-number validation
# Accepts any of:
#   +85512345678   (full international)
#   85512345678    (country code, no +)
#   012345678      (local with leading 0)
#   12345678       (local without leading 0)
# The local subscriber number must be 8–9 digits.
_KH_FULL_RE = re.compile(r"^\+?855(0?)(\d{8,9})$")
_KH_LOCAL_RE = re.compile(r"^0?(\d{8,9})$")

# API endpoint
_SMS_ENDPOINT = "/sms/2/text/advanced"

# HTTP client timeout (seconds)
_HTTP_TIMEOUT = 15.0

# Bot start time (UTC)
_BOT_START_TIME: datetime = datetime.now(timezone.utc)

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)

_log_formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = RotatingFileHandler(
    "logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_formatter)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_log_formatter)

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), handlers=[_file_handler, _console_handler])

logger = logging.getLogger("sms_bot")

# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class UserStats:
    """Per-user statistics and rate-limit state."""

    user_id: int
    username: str = ""
    total_sent: int = 0
    total_failed: int = 0
    # Timestamps (UNIX seconds) of recent sends within the current window
    send_timestamps: Deque[float] = field(default_factory=deque)

    def is_rate_limited(self) -> bool:
        """Return True if the user has exceeded the rolling-window limit."""
        now = time.monotonic()
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        # Purge old timestamps
        while self.send_timestamps and self.send_timestamps[0] < cutoff:
            self.send_timestamps.popleft()
        return len(self.send_timestamps) >= RATE_LIMIT_MAX_MESSAGES

    def record_send(self) -> None:
        """Record a successful send timestamp."""
        self.send_timestamps.append(time.monotonic())
        self.total_sent += 1

    def record_failure(self) -> None:
        """Increment failure counter (does NOT consume rate-limit quota)."""
        self.total_failed += 1

    def remaining_quota(self) -> int:
        """Return how many sends remain in the current window."""
        now = time.monotonic()
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        while self.send_timestamps and self.send_timestamps[0] < cutoff:
            self.send_timestamps.popleft()
        return max(0, RATE_LIMIT_MAX_MESSAGES - len(self.send_timestamps))

    def seconds_until_reset(self) -> int:
        """Return approximate seconds until the oldest slot expires."""
        if not self.send_timestamps:
            return 0
        oldest = self.send_timestamps[0]
        reset_at = oldest + RATE_LIMIT_WINDOW_SECONDS
        return max(0, int(reset_at - time.monotonic()))


# Global in-memory user stats registry
_user_stats: Dict[int, UserStats] = {}


def get_user_stats(user_id: int, username: str = "") -> UserStats:
    if user_id not in _user_stats:
        _user_stats[user_id] = UserStats(user_id=user_id, username=username)
    return _user_stats[user_id]


# ─────────────────────────────────────────────────────────────────────────────
# Phone number utilities
# ─────────────────────────────────────────────────────────────────────────────


def normalize_phone(raw: str) -> Optional[str]:
    """
    Normalise a Cambodian phone number to E.164 format (+855XXXXXXXXX).

    Returns the formatted number or None if the input is invalid.
    """
    cleaned = raw.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    # Try full international format first (+855 or 855)
    m = _KH_FULL_RE.match(cleaned)
    if m:
        subscriber = m.group(2)
        # Subscriber number must be 8 digits (standard KH local length)
        if 8 <= len(subscriber) <= 9:
            return f"+855{subscriber}"

    # Try local format (0XXXXXXXX or XXXXXXXX)
    m = _KH_LOCAL_RE.match(cleaned)
    if m:
        subscriber = m.group(1)
        if 8 <= len(subscriber) <= 9:
            return f"+855{subscriber}"

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Infobip API client
# ─────────────────────────────────────────────────────────────────────────────


async def send_sms(to: str, text: str) -> dict:
    """
    Send an SMS via the Infobip SMS API (v2).

    Args:
        to:   E.164 destination number (e.g. "+85512345678").
        text: The SMS body text.

    Returns:
        The parsed JSON response from the Infobip API.

    Raises:
        httpx.TimeoutException:  If the API request times out.
        httpx.HTTPStatusError:   If the API returns a non-2xx status.
        httpx.RequestError:      For any other network-level error.
    """
    url = f"https://{INFOBIP_BASE_URL}{_SMS_ENDPOINT}"
    headers = {
        "Authorization": f"App {INFOBIP_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "messages": [
            {
                "from": INFOBIP_SENDER,
                "destinations": [{"to": to}],
                "text": text,
            }
        ]
    }

    logger.debug("Sending SMS to %s via %s", to, url)

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    logger.info("SMS sent to %s | messageId=%s", to, _extract_message_id(data))
    return data


def _extract_message_id(response_data: dict) -> str:
    """Extract the first messageId from an Infobip SMS API response."""
    try:
        return response_data["messages"][0]["messageId"]
    except (KeyError, IndexError, TypeError):
        return "unknown"


def _extract_status(response_data: dict) -> str:
    """Extract the human-readable status description from an Infobip SMS API response."""
    try:
        return response_data["messages"][0]["status"]["description"]
    except (KeyError, IndexError, TypeError):
        return "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Telegram command handlers
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command – send a welcome message."""
    user = update.effective_user
    logger.info("/start from user_id=%s username=%s", user.id, user.username)

    text = (
        f"👋 Hello, *{user.first_name}*!\n\n"
        "I'm your *Infobip SMS Bot* 🤖\n"
        "I can send SMS messages to Cambodian phone numbers **(+855)**.\n\n"
        "📌 *Quick start:*\n"
        "  • /sendsms – Send an SMS right now\n"
        "  • /help    – Full command reference\n"
        "  • /status  – Your stats & rate-limit info\n\n"
        "Let's get started! Type /sendsms to send your first message."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command – display full command reference."""
    user = update.effective_user
    logger.info("/help from user_id=%s", user.id)

    text = (
        "📖 *Help – Infobip SMS Bot*\n\n"
        "*Commands:*\n"
        "  /start    – Welcome message\n"
        "  /help     – This help text\n"
        "  /sendsms  – Send an SMS (step-by-step wizard)\n"
        "  /status   – Show your stats & rate-limit status\n"
        "  /cancel   – Cancel the current /sendsms wizard\n\n"
        "*Phone number format (Cambodia +855):*\n"
        "  You may enter the number in any of these ways:\n"
        "  • `+85512345678`  ← full international\n"
        "  • `85512345678`   ← country code, no +\n"
        "  • `012345678`     ← local with leading 0\n"
        "  • `12345678`      ← local without leading 0\n\n"
        "*Rate limiting:*\n"
        f"  Maximum *{RATE_LIMIT_MAX_MESSAGES}* SMS messages per "
        f"*{RATE_LIMIT_WINDOW_SECONDS}* seconds per user.\n\n"
        "*SMS text:*\n"
        "  Up to 160 characters for a single SMS segment.\n"
        "  Longer messages are split automatically by the carrier."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /status command – show uptime and per-user stats."""
    user = update.effective_user
    stats = get_user_stats(user.id, user.username or "")
    logger.info("/status from user_id=%s", user.id)

    uptime_seconds = int((datetime.now(timezone.utc) - _BOT_START_TIME).total_seconds())
    uptime_str = _format_uptime(uptime_seconds)

    rate_limited = stats.is_rate_limited()
    quota_remaining = stats.remaining_quota()
    reset_in = stats.seconds_until_reset()

    status_icon = "🔴 Rate-limited" if rate_limited else "🟢 OK"

    text = (
        "📊 *Bot & User Status*\n\n"
        f"🤖 *Bot uptime:* {uptime_str}\n\n"
        f"👤 *Your stats (@{user.username or 'N/A'}):*\n"
        f"  • Messages sent:   *{stats.total_sent}*\n"
        f"  • Failures:        *{stats.total_failed}*\n"
        f"  • Status:          {status_icon}\n"
        f"  • Quota remaining: *{quota_remaining}* / {RATE_LIMIT_MAX_MESSAGES} "
        f"(per {RATE_LIMIT_WINDOW_SECONDS}s)\n"
    )
    if rate_limited:
        text += f"  • Reset in approx: *{reset_in}s*\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


def _format_uptime(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# /sendsms conversation wizard
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_sendsms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point for the /sendsms conversation.
    Asks the user for a destination phone number.
    """
    user = update.effective_user
    stats = get_user_stats(user.id, user.username or "")
    logger.info("/sendsms started by user_id=%s", user.id)

    # Rate-limit check before starting the wizard
    if stats.is_rate_limited():
        reset_in = stats.seconds_until_reset()
        await update.message.reply_text(
            f"⏳ *Rate limit reached.*\n"
            f"You can send at most *{RATE_LIMIT_MAX_MESSAGES}* messages "
            f"every *{RATE_LIMIT_WINDOW_SECONDS}* seconds.\n"
            f"Please wait approximately *{reset_in}s* before trying again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    # Clear any leftover wizard data
    context.user_data.clear()

    await update.message.reply_text(
        "📱 *Step 1 of 2 – Phone number*\n\n"
        "Please enter the *Cambodian (+855)* destination phone number.\n\n"
        "Accepted formats:\n"
        "  • `+85512345678`\n"
        "  • `012345678`\n"
        "  • `12345678`\n\n"
        "Type /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return STEP_PHONE


async def wizard_receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    STEP_PHONE handler – validate and store the destination number,
    then ask for the SMS text.
    """
    raw_phone = update.message.text.strip()
    normalized = normalize_phone(raw_phone)

    if normalized is None:
        await update.message.reply_text(
            "❌ *Invalid phone number.*\n\n"
            "Only Cambodian (+855) numbers are supported.\n"
            "Please re-enter a valid number or type /cancel to abort.\n\n"
            "Examples: `+85512345678`, `012345678`, `12345678`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return STEP_PHONE  # Stay in the same step

    context.user_data["phone"] = normalized
    logger.debug("user_id=%s entered phone %s → normalised to %s", update.effective_user.id, raw_phone, normalized)

    await update.message.reply_text(
        f"✅ Number accepted: `{normalized}`\n\n"
        "✉️ *Step 2 of 2 – SMS message*\n\n"
        "Please type the SMS text you want to send "
        "(up to 160 characters for a single segment).\n\n"
        "Type /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return STEP_TEXT


async def wizard_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    STEP_TEXT handler – validate the text, send the SMS via Infobip,
    and report the result.
    """
    user = update.effective_user
    stats = get_user_stats(user.id, user.username or "")
    sms_text = update.message.text.strip()

    if not sms_text:
        await update.message.reply_text(
            "❌ The SMS text cannot be empty. Please enter the message text.",
        )
        return STEP_TEXT

    phone: Optional[str] = context.user_data.get("phone")
    if not phone:
        # Defensive: this shouldn't normally happen
        await update.message.reply_text("⚠️ Session error – please start again with /sendsms.")
        context.user_data.clear()
        return ConversationHandler.END

    # Final rate-limit check (window may have filled up while user typed)
    if stats.is_rate_limited():
        reset_in = stats.seconds_until_reset()
        await update.message.reply_text(
            f"⏳ *Rate limit reached* while composing your message.\n"
            f"Please wait approximately *{reset_in}s* and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Confirmation keyboard
    confirm_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Send", callback_data="confirm_send"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_send"),
            ]
        ]
    )
    context.user_data["sms_text"] = sms_text

    preview = sms_text if len(sms_text) <= 80 else sms_text[:77] + "…"
    await update.message.reply_text(
        f"📋 *Confirm SMS*\n\n"
        f"📱 *To:*      `{phone}`\n"
        f"📝 *Message:* `{preview}`\n"
        f"📏 *Length:*  {len(sms_text)} character(s)\n\n"
        "Please confirm or cancel.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirm_keyboard,
    )
    return STEP_TEXT  # Wait for inline button callback


async def callback_confirm_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Inline-button callback – the user pressed "Send".
    Dispatches the SMS and ends the conversation.
    """
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    stats = get_user_stats(user.id, user.username or "")
    phone: Optional[str] = context.user_data.get("phone")
    sms_text: Optional[str] = context.user_data.get("sms_text")

    if not phone or not sms_text:
        await query.edit_message_text("⚠️ Session expired – please start again with /sendsms.")
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text("⏳ Sending your SMS… please wait.")
    logger.info("Dispatching SMS | user_id=%s | to=%s | len=%d", user.id, phone, len(sms_text))

    try:
        result = await send_sms(phone, sms_text)
        status_desc = _extract_status(result)
        message_id = _extract_message_id(result)
        stats.record_send()

        await query.edit_message_text(
            f"✅ *SMS sent successfully!*\n\n"
            f"📱 *To:*        `{phone}`\n"
            f"🆔 *Message ID:* `{message_id}`\n"
            f"📬 *Status:*     {status_desc}\n\n"
            f"📊 You have *{stats.remaining_quota()}* send(s) remaining "
            f"in the current {RATE_LIMIT_WINDOW_SECONDS}s window.",
            parse_mode=ParseMode.MARKDOWN,
        )
        logger.info("SMS delivered | user_id=%s | to=%s | messageId=%s | status=%s", user.id, phone, message_id, status_desc)

    except httpx.TimeoutException:
        stats.record_failure()
        logger.error("Infobip API timeout | user_id=%s | to=%s", user.id, phone)
        await query.edit_message_text(
            "⏰ *Request timed out.*\n\n"
            "The Infobip API did not respond in time. "
            "Please check your internet connection and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )

    except httpx.HTTPStatusError as exc:
        stats.record_failure()
        status_code = exc.response.status_code
        logger.error("Infobip HTTP error %d | user_id=%s | to=%s | body=%s", status_code, user.id, phone, exc.response.text[:200])

        if status_code == 401:
            msg = "🔐 *Authorisation failed.* Your Infobip API key is invalid or expired."
        elif status_code == 400:
            msg = "⚠️ *Bad request.* The phone number or message text was rejected by Infobip."
        elif status_code == 429:
            msg = "⏳ *Infobip rate limit exceeded.* Please try again later."
        elif 500 <= status_code < 600:
            msg = f"🔥 *Infobip server error* ({status_code}). Please try again later."
        else:
            msg = f"❌ *API error {status_code}.* Please try again later."

        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)

    except httpx.RequestError as exc:
        stats.record_failure()
        logger.error("Network error | user_id=%s | to=%s | error=%s", user.id, phone, exc)
        await query.edit_message_text(
            "🌐 *Network error.*\n\n"
            "Could not reach the Infobip API. "
            "Please check your internet connection and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as exc:  # noqa: BLE001 – catch-all so wizard always terminates cleanly
        stats.record_failure()
        logger.exception("Unexpected error during SMS send | user_id=%s | to=%s", user.id, phone)
        await query.edit_message_text(
            "💥 *An unexpected error occurred.*\n"
            "The error has been logged. Please try again later.",
            parse_mode=ParseMode.MARKDOWN,
        )

    finally:
        context.user_data.clear()

    return ConversationHandler.END


async def callback_cancel_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inline-button callback – the user pressed "Cancel"."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ SMS cancelled. Type /sendsms to start again.")
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /cancel command – abort the wizard at any step.
    Also works outside the conversation (no-op).
    """
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Wizard cancelled. Type /sendsms when you're ready to send an SMS.",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Fallback / unknown command handler
# ─────────────────────────────────────────────────────────────────────────────


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unknown commands gracefully."""
    await update.message.reply_text(
        "❓ Unknown command. Type /help to see the available commands.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Error handler
# ─────────────────────────────────────────────────────────────────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled exceptions and notify the user where possible."""
    logger.error("Unhandled exception", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "💥 An unexpected error occurred. Please try again later.",
            )
        except Exception:
            pass  # Don't propagate errors in the error handler itself


# ─────────────────────────────────────────────────────────────────────────────
# Application assembly
# ─────────────────────────────────────────────────────────────────────────────


def build_application() -> Application:
    """Construct and configure the Telegram Application."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation handler for /sendsms wizard
    sendsms_conv = ConversationHandler(
        entry_points=[CommandHandler("sendsms", cmd_sendsms)],
        states={
            STEP_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_phone),
            ],
            STEP_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_text),
                CallbackQueryHandler(callback_confirm_send, pattern="^confirm_send$"),
                CallbackQueryHandler(callback_cancel_send, pattern="^cancel_send$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(sendsms_conv)
    # /cancel outside the conversation is a no-op but gives friendly feedback
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    # Catch-all for unrecognised commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    app.add_error_handler(error_handler)

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Start the bot using long polling."""
    logger.info("Starting Infobip SMS Telegram Bot")
    logger.info("Infobip base URL: %s", INFOBIP_BASE_URL)
    logger.info(
        "Rate limit: %d messages / %ds per user",
        RATE_LIMIT_MAX_MESSAGES,
        RATE_LIMIT_WINDOW_SECONDS,
    )

    app = build_application()

    # run_polling blocks until interrupted
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
