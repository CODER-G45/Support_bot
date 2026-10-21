# bot.py - Simple Support Bot with per-user "chat folders"
#
# How it works
# ------------
# - A normal user messages the bot -> the message is forwarded to YOU
#   (the admin) with a header showing the sender's name/username/ID.
# - Under every forwarded message there's a "💬 Reply" button. Tap it,
#   type your reply, and it's sent straight to that user only.
# - Every message (both directions) is saved per-user, so tapping
#   "🗂 View Chat" (or /chat <user_id>) shows that one user's full
#   conversation on its own — nothing from other users mixed in.
# - Multiple users messaging at once never collide: each message carries
#   its own Reply button tied to that specific user_id, and your "who am I
#   replying to" state is cleared the instant you send a reply.
#
# Setup
# -----
# 1. pip install python-telegram-bot==21.* python-dotenv
# 2. Copy .env.example to .env and fill in BOT_TOKEN and ADMIN_ID
#    (ADMIN_ID = your own numeric Telegram user ID, get it from @userinfobot)
# 3. python bot.py

import os
import json
import html
import logging
from datetime import datetime
from typing import Dict, List

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.error import TelegramError

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Set BOT_TOKEN and ADMIN_ID in your .env file (see .env.example).")

DATA_FILE = "support_data.json"
MAX_HISTORY_SHOWN = 40   # messages shown per /chat view, most recent

# ============================== STORAGE ==============================
# {
#   "users": {
#       "<user_id>": {"name": "...", "username": "...", "first_seen": "..."}
#   },
#   "threads": {
#       "<user_id>": [ {"from": "user"|"admin", "text": "...", "time": "..."} ]
#   }
# }

def _load() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("support_data.json unreadable, starting fresh.")
    return {"users": {}, "threads": {}}

def _save(data: dict):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

db = _load()

def remember_user(user):
    uid = str(user.id)
    db["users"].setdefault(uid, {})
    db["users"][uid].update({
        "name": user.full_name or "",
        "username": user.username or "",
        "first_seen": db["users"][uid].get("first_seen", datetime.utcnow().isoformat(timespec="seconds") + "Z"),
    })
    _save(db)

def log_message(user_id: int, sender: str, text: str):
    uid = str(user_id)
    db["threads"].setdefault(uid, [])
    db["threads"][uid].append({
        "from": sender,
        "text": text,
        "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })
    # keep the stored history from growing forever
    db["threads"][uid] = db["threads"][uid][-500:]
    _save(db)

def user_label(user_id: int) -> str:
    u = db["users"].get(str(user_id), {})
    name = u.get("name") or f"User {user_id}"
    uname = f" (@{u['username']})" if u.get("username") else ""
    return f"{name}{uname}"

# ============================== KEYBOARDS ==============================

def reply_button(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply", callback_data=f"reply_{user_id}"),
        InlineKeyboardButton("🗂 View Chat", callback_data=f"view_{user_id}"),
    ]])

def cancel_reply_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel reply", callback_data="cancel_reply")]])

# ============================== USER SIDE ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text(
            "👋 This is your Support inbox.\n\n"
            "Whenever someone messages the bot, you'll get it here with a "
            "Reply button. Use /chat <user_id> any time to see one user's "
            "full history on its own."
        )
        return
    await update.message.reply_text(
        "👋 Hi! Send us your question or issue and our team will reply here."
    )

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any text from a normal (non-admin) user -> relay to admin as that user's
    single, always-up-to-date "folder" message (old one is replaced, not piled
    up) with a Reply button."""
    user = update.effective_user
    text = update.message.text or update.message.caption or "[non-text message]"

    uid = str(user.id)
    is_first_message = not db["threads"].get(uid)

    remember_user(user)
    log_message(user.id, "user", text)

    total = len(db["threads"].get(uid, []))
    header = (
        f"📩 Support message ({total} total)\n"
        f"👤 {html.escape(user.full_name)}"
        + (f" (@{html.escape(user.username)})" if user.username else "")
        + f"\n🆔 <code>{user.id}</code>\n\n"
        + html.escape(text)
    )

    # Replace this user's previous "folder" message instead of stacking a
    # new one for every single message they send.
    old_msg_id = db["users"][uid].get("admin_message_id")
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id=ADMIN_ID, message_id=old_msg_id)
        except TelegramError:
            pass  # already gone / too old — fine, we're sending a fresh one anyway

    try:
        sent = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=header,
            parse_mode="HTML",
            reply_markup=reply_button(user.id),
        )
    except TelegramError as e:
        logger.error(f"Could not forward message from {user.id} to admin: {e}")
        return

    db["users"][uid]["admin_message_id"] = sent.message_id
    _save(db)

    if is_first_message:
        await update.message.reply_text("✅ Got it — we'll reply here shortly.")

# ============================== ADMIN SIDE ==============================

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer()
        return
    data = query.data
    await query.answer()

    if data == "cancel_reply":
        context.user_data.pop("replying_to", None)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data.startswith("reply_"):
        target_id = int(data.split("_", 1)[1])
        context.user_data["replying_to"] = target_id
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"✏️ Type your reply to {user_label(target_id)} now:",
            reply_markup=cancel_reply_keyboard(),
        )
        return

    if data.startswith("view_"):
        target_id = int(data.split("_", 1)[1])
        await send_chat_history(context, target_id)
        return

async def send_chat_history(context: ContextTypes.DEFAULT_TYPE, target_id: int):
    thread = db["threads"].get(str(target_id), [])
    if not thread:
        await context.bot.send_message(ADMIN_ID, "No messages from this user yet.")
        return
    recent = thread[-MAX_HISTORY_SHOWN:]
    lines = [f"🗂 Chat with {user_label(target_id)} (ID: {target_id})\n"]
    for m in recent:
        tag = "🙋 User" if m["from"] == "user" else "🧑‍💼 You"
        lines.append(f"[{m['time'][11:16]}] {tag}: {m['text']}")
    if len(thread) > MAX_HISTORY_SHOWN:
        lines.insert(1, f"(showing last {MAX_HISTORY_SHOWN} of {len(thread)} messages)\n")
    await context.bot.send_message(
        ADMIN_ID,
        html.escape("\n".join(lines)),
        reply_markup=reply_button(target_id),
    )

async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text from the admin. If a reply is pending, send it to that user."""
    target_id = context.user_data.get("replying_to")
    if not target_id:
        await update.message.reply_text(
            "Tap 💬 Reply under a user's message first, or use /chat <user_id>."
        )
        return

    text = update.message.text
    try:
        await context.bot.send_message(chat_id=target_id, text=text)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Could not deliver to that user: {e}")
        return

    log_message(target_id, "admin", text)
    context.user_data.pop("replying_to", None)
    await update.message.reply_text(f"✅ Sent to {user_label(target_id)}.")

async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/chat <user_id> - view one user's history on demand."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /chat <user_id>")
        return
    await send_chat_history(context, int(context.args[0]))

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/users - list everyone who has messaged the bot."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not db["users"]:
        await update.message.reply_text("No users yet.")
        return
    lines = ["👥 Users:\n"]
    for uid, u in db["users"].items():
        uname = f" (@{u['username']})" if u.get("username") else ""
        lines.append(f"• {u.get('name', '—')}{uname} — ID: {uid}")
    await update.message.reply_text("\n".join(lines))

# ============================== ROUTER ==============================

async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await handle_admin_text(update, context)
    else:
        await handle_user_message(update, context)

# ============================== MAIN ==============================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chat", chat_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CallbackQueryHandler(handle_admin_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    # Non-text (photos/docs/voice) from users: forward a placeholder note too
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.VIDEO) & ~filters.User(ADMIN_ID),
        handle_user_message
    ))

    logger.info("Support bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
