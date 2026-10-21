# bot.py - Support Bot v2 (Render-ready)
#
# Flow
# ----
# User messages the bot -> it lands in YOUR (admin) chat with action buttons.
# You reply via the button OR by simply swiping/replying to the message.
# Everything is stored per user, so each user has their own "folder".
#
# New in v2
# ---------
# * Photos / files / voice / video / stickers both ways (not just text)
# * Swipe-reply to any forwarded message (no need to tap Reply first)
# * Tickets: Open / Closed, /open inbox, close + reopen buttons
# * Satisfaction rating (1-5 stars) after a ticket is closed
# * Ban / unban, anti-spam rate limit
# * /broadcast (with confirmation), /find, /stats, /users, /setwelcome
# * /backup + /restore + auto-backup (Render free disk is ephemeral!)
# * Built-in web server (/health) + self-ping -> UptimeRobot keeps it awake
#
# Run:  python bot.py   (needs BOT_TOKEN and ADMIN_ID, see .env.example)

import os
import json
import html
import time
import asyncio
import logging
import threading
from io import BytesIO
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeChat,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, RetryAfter, NetworkError, Conflict
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("supportbot")

# ============================== CONFIG ==============================

BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Set BOT_TOKEN and ADMIN_ID (env vars or .env file).")

PORT = int(os.getenv("PORT", "10000"))                       # Render sets this
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "support_data.json")
SELF_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL")  # auto on Render
BACKUP_HOURS = float(os.getenv("BACKUP_HOURS", "12"))        # 0 = disable
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "6"))               # msgs ...
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "20"))            # ... per N seconds
MAX_HISTORY_SHOWN = 40
MAX_THREAD_STORED = 500

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
except Exception:
    TZ = timezone.utc

DEFAULT_WELCOME = "👋 Hi! Send us your question or issue and our team will reply here."
START_TIME = time.time()

# ============================== STORAGE ==============================
# db = {
#   "users":    {uid: {name, username, first_seen, last_active, status,
#                      banned, admin_message_id, awaiting_rating}},
#   "threads":  {uid: [{from, text, time}]},
#   "msg_map":  {admin_chat_message_id: uid},   # makes swipe-reply work
#   "settings": {welcome},
#   "ratings":  [{user, stars, time}],
# }

_dirty = False
_version = 0          # bumped on every change (used by auto-backup)
_fresh_start = not os.path.exists(DATA_FILE)


def _normalize(d: dict) -> dict:
    d.setdefault("users", {})
    d.setdefault("threads", {})
    d.setdefault("msg_map", {})
    d.setdefault("settings", {}).setdefault("welcome", DEFAULT_WELCOME)
    d.setdefault("ratings", [])
    return d


def _load() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return _normalize(json.load(f))
        except (json.JSONDecodeError, OSError):
            logger.warning("Data file unreadable, starting fresh.")
    return _normalize({})


def flush(force: bool = False):
    """Write db to disk (atomic). Normally called by the autosave loop."""
    global _dirty
    if not _dirty and not force:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)
    _dirty = False


def mark_dirty():
    global _dirty, _version
    _dirty = True
    _version += 1


db = _load()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_local(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def fmt_time(iso: str) -> str:
    try:
        return to_local(iso).strftime("%d %b %H:%M")
    except Exception:
        return iso


def remember_user(user):
    uid = str(user.id)
    u = db["users"].setdefault(uid, {"first_seen": now_iso(), "status": "open"})
    u["name"] = user.full_name or ""
    u["username"] = user.username or ""
    u["last_active"] = now_iso()
    mark_dirty()


def log_message(user_id: int, sender: str, text: str):
    uid = str(user_id)
    thread = db["threads"].setdefault(uid, [])
    thread.append({"from": sender, "text": text, "time": now_iso()})
    db["threads"][uid] = thread[-MAX_THREAD_STORED:]
    mark_dirty()


def map_msg(message_id: int, user_id: int):
    db["msg_map"][str(message_id)] = user_id
    if len(db["msg_map"]) > 3000:                    # keep it bounded
        for k in list(db["msg_map"])[:500]:
            db["msg_map"].pop(k, None)
    mark_dirty()


def user_label(user_id: int) -> str:
    u = db["users"].get(str(user_id), {})
    name = u.get("name") or f"User {user_id}"
    uname = f" (@{u['username']})" if u.get("username") else ""
    return f"{name}{uname}"


def describe(msg) -> str:
    """Short text description of any kind of message (for logs/headers)."""
    kind = None
    if msg.photo:
        kind = "📷 Photo"
    elif msg.document:
        kind = "📄 File: " + (msg.document.file_name or "document")
    elif msg.voice:
        kind = "🎤 Voice message"
    elif msg.video:
        kind = "🎬 Video"
    elif msg.video_note:
        kind = "⏺ Video note"
    elif msg.audio:
        kind = "🎵 Audio"
    elif msg.sticker:
        kind = "🏷 Sticker " + (msg.sticker.emoji or "")
    elif msg.location:
        kind = "📍 Location"
    elif msg.contact:
        kind = "👤 Contact"
    text = msg.text or msg.caption or ""
    if kind and text:
        return f"{kind} — {text}"
    return kind or text or "[unsupported message]"


# ============================== HELPERS ==============================

_tasks: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


def chunk_lines(lines, limit: int = 3800):
    chunks, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


_hits = defaultdict(deque)
_last_warn: dict = {}


def rate_check(uid: int) -> str:
    """'ok' | 'warn' (first drop in window) | 'drop'"""
    now = time.monotonic()
    q = _hits[uid]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        if uid not in _last_warn or now - _last_warn[uid] > RATE_WINDOW:
            _last_warn[uid] = now
            return "warn"
        return "drop"
    q.append(now)
    return "ok"


# ============================== KEYBOARDS ==============================

def action_keyboard(uid: int) -> InlineKeyboardMarkup:
    u = db["users"].get(str(uid), {})
    closed = u.get("status") == "closed"
    banned = bool(u.get("banned"))
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Reply", callback_data=f"reply_{uid}"),
            InlineKeyboardButton("🗂 View Chat", callback_data=f"view_{uid}"),
        ],
        [
            InlineKeyboardButton("🔓 Reopen" if closed else "✅ Close",
                                 callback_data=f"{'reopen' if closed else 'close'}_{uid}"),
            InlineKeyboardButton("♻️ Unban" if banned else "🚫 Ban",
                                 callback_data=f"{'unban' if banned else 'ban'}_{uid}"),
        ],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_reply")]])


def rating_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐" * n if n < 4 else f"{n}⭐", callback_data=f"rate_{n}")
        for n in range(1, 6)
    ]])


# ============================== USER SIDE ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == ADMIN_ID:
        await update.message.reply_text(
            "👋 This is your Support inbox.\n\n"
            "Users' messages arrive here with buttons. Reply with the 💬 button "
            "or just swipe-reply to a message.\n\nSend /help for all commands."
        )
        return
    if db["users"].get(str(user.id), {}).get("banned"):
        return
    await update.message.reply_text(db["settings"]["welcome"])


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(db["settings"]["welcome"])
        return
    await update.message.reply_text(
        "🛠 Admin commands\n\n"
        "/open – tickets waiting for you\n"
        "/chat <id> – one user's full history\n"
        "/find <text> – search name / username / id / messages\n"
        "/users – recent users\n"
        "/stats – numbers at a glance\n"
        "/ban <id>  /unban <id>\n"
        "/broadcast <text> – message all users (asks to confirm)\n"
        "/setwelcome <text> – change welcome message (/setwelcome reset)\n"
        "/backup – download data file\n"
        "Send a .json backup with caption /restore to restore it.\n\n"
        "Tip: swipe-reply to any user message to answer them."
    )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    uid = str(user.id)
    if db["users"].get(uid, {}).get("banned"):
        return

    verdict = rate_check(user.id)
    if verdict != "ok":
        if verdict == "warn":
            await msg.reply_text("⏳ You're sending messages too fast. Please slow down a little.")
        return

    remember_user(user)
    u = db["users"][uid]
    is_first = not db["threads"].get(uid)
    reopened = u.get("status") == "closed"
    u["status"] = "open"
    u["awaiting_rating"] = False

    desc = describe(msg)
    log_message(user.id, "user", desc)
    total = len(db["threads"][uid])

    header = (
        f"📩 <b>{'Reopened ticket' if reopened else 'New message'}</b> · {total} in thread\n"
        f"👤 {html.escape(user.full_name or 'Unknown')}"
        + (f" (@{html.escape(user.username)})" if user.username else "")
        + f"\n🆔 <code>{user.id}</code>\n\n"
        + html.escape(desc[:3000])
    )

    # Replace this user's previous "header" so the inbox stays tidy.
    old_id = u.get("admin_message_id")
    if old_id:
        try:
            await context.bot.delete_message(chat_id=ADMIN_ID, message_id=old_id)
        except TelegramError:
            pass

    try:
        if not msg.text:  # media: copy the original so you see/hear it
            try:
                copied = await context.bot.copy_message(ADMIN_ID, msg.chat_id, msg.message_id)
                map_msg(copied.message_id, user.id)
            except TelegramError as e:
                logger.warning(f"Could not copy media from {user.id}: {e}")
        sent = await context.bot.send_message(
            chat_id=ADMIN_ID, text=header, parse_mode=ParseMode.HTML,
            reply_markup=action_keyboard(user.id),
        )
    except TelegramError as e:
        logger.error(f"Could not forward message from {user.id} to admin: {e}")
        await msg.reply_text("⚠️ Sorry, we couldn't deliver your message right now. Please try again shortly.")
        return

    u["admin_message_id"] = sent.message_id
    map_msg(sent.message_id, user.id)

    if is_first:
        await msg.reply_text("✅ Got it — we'll reply here shortly.")
    elif reopened:
        await msg.reply_text("🔄 Your ticket has been reopened. We'll get back to you soon.")


# ============================== ADMIN SIDE ==============================

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends anything (text/photo/file/voice...) -> goes to the right user."""
    msg = update.effective_message
    target, via_button = None, False
    if msg.reply_to_message:
        target = db["msg_map"].get(str(msg.reply_to_message.message_id))
    if not target:
        target = context.user_data.get("replying_to")
        via_button = bool(target)
    if not target:
        await msg.reply_text("Swipe-reply to a user's message, tap 💬 Reply, or use /chat <user_id>.")
        return

    try:
        await context.bot.copy_message(chat_id=target, from_chat_id=msg.chat_id, message_id=msg.message_id)
    except Forbidden:
        await msg.reply_text("❌ That user has blocked the bot.")
        return
    except TelegramError as e:
        await msg.reply_text(f"❌ Could not deliver: {e}")
        return

    log_message(target, "admin", describe(msg))
    if via_button:
        context.user_data.pop("replying_to", None)
    await msg.reply_text(f"✅ Sent to {user_label(target)}.")


async def send_chat_history(bot, target_id: int):
    thread = db["threads"].get(str(target_id), [])
    if not thread:
        await bot.send_message(ADMIN_ID, "No messages from this user yet.")
        return
    recent = thread[-MAX_HISTORY_SHOWN:]
    lines = [f"🗂 <b>Chat with {html.escape(user_label(target_id))}</b> (<code>{target_id}</code>)"]
    if len(thread) > MAX_HISTORY_SHOWN:
        lines.append(f"<i>showing last {MAX_HISTORY_SHOWN} of {len(thread)} messages</i>")
    lines.append("")
    for m in recent:
        tag = "🙋" if m["from"] == "user" else "🧑‍💼 You"
        lines.append(f"<code>{fmt_time(m['time'])}</code> {tag}: {html.escape(m['text'])}")
    chunks = chunk_lines(lines)
    for i, c in enumerate(chunks):
        last = i == len(chunks) - 1
        await bot.send_message(
            ADMIN_ID, c, parse_mode=ParseMode.HTML,
            reply_markup=action_keyboard(target_id) if last else None,
        )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    if data.startswith("rate_"):
        await handle_rating(q, context)
        return
    if q.from_user.id != ADMIN_ID:
        await q.answer()
        return

    action, _, arg = data.partition("_")

    if action == "cancel":
        context.user_data.pop("replying_to", None)
        await q.answer("Cancelled")
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return
    if action == "bc":
        await handle_broadcast_choice(q, context, arg)
        return
    if not arg.isdigit() or str(int(arg)) not in db["users"]:
        await q.answer("Unknown user", show_alert=True)
        return

    uid = int(arg)
    u = db["users"][str(uid)]

    if action == "reply":
        context.user_data["replying_to"] = uid
        await q.answer()
        await context.bot.send_message(
            ADMIN_ID, f"✏️ Type your reply to {user_label(uid)} now (text, photo, file, voice…):",
            reply_markup=cancel_keyboard(),
        )
        return
    if action == "view":
        await q.answer()
        await send_chat_history(context.bot, uid)
        return

    toast = ""
    if action == "close":
        u["status"], u["awaiting_rating"] = "closed", True
        toast = "Ticket closed"
        try:
            await context.bot.send_message(
                uid,
                "✅ Your issue has been marked as resolved. Message us anytime to reopen it.\n\n"
                "How was your experience?",
                reply_markup=rating_keyboard(),
            )
        except TelegramError:
            pass
    elif action == "reopen":
        u["status"] = "open"
        toast = "Ticket reopened"
    elif action == "ban":
        u["banned"] = True
        toast = "User banned"
    elif action == "unban":
        u["banned"] = False
        toast = "User unbanned"
    else:
        await q.answer()
        return

    mark_dirty()
    await q.answer(toast)
    try:
        await q.edit_message_reply_markup(reply_markup=action_keyboard(uid))
    except TelegramError:
        pass


async def handle_rating(q, context):
    uid = str(q.from_user.id)
    u = db["users"].get(uid)
    if not u or not u.get("awaiting_rating"):
        await q.answer("Already rated 🙂")
        return
    try:
        stars = max(1, min(5, int(q.data.split("_")[1])))
    except (ValueError, IndexError):
        await q.answer()
        return
    u["awaiting_rating"] = False
    db["ratings"].append({"user": int(uid), "stars": stars, "time": now_iso()})
    mark_dirty()
    await q.answer("Thanks!")
    try:
        await q.edit_message_text(f"Thanks for your feedback! {'⭐' * stars}")
    except TelegramError:
        pass
    try:
        await context.bot.send_message(
            ADMIN_ID, f"⭐ {user_label(int(uid))} rated the support {stars}/5",
            disable_notification=True,
        )
    except TelegramError:
        pass


# ---------- commands ----------

async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /chat <user_id>")
        return
    await send_chat_history(context.bot, int(context.args[0]))


async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = []
    for uid, u in db["users"].items():
        thread = db["threads"].get(uid)
        if u.get("status", "open") == "open" and thread and not u.get("banned"):
            rows.append((u.get("last_active", ""), uid, thread[-1]))
    if not rows:
        await update.message.reply_text("🎉 No open tickets. Inbox zero!")
        return
    rows.sort(reverse=True)
    buttons = []
    for _, uid, last in rows[:20]:
        dot = "🔴" if last["from"] == "user" else "🟡"
        name = (db["users"][uid].get("name") or uid)[:18]
        preview = last["text"].replace("\n", " ")[:22]
        buttons.append([InlineKeyboardButton(f"{dot} {name}: {preview}", callback_data=f"view_{uid}")])
    await update.message.reply_text(
        f"📥 {len(rows)} open ticket(s)\n🔴 waiting for your reply · 🟡 you replied last",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db["users"]:
        await update.message.reply_text("No users yet.")
        return
    items = sorted(db["users"].items(), key=lambda kv: kv[1].get("last_active", ""), reverse=True)
    lines = [f"👥 <b>{len(items)} users</b> (latest 40)\n"]
    for uid, u in items[:40]:
        icon = "🚫" if u.get("banned") else ("⚪" if u.get("status") == "closed" else "🟢")
        uname = f" (@{html.escape(u['username'])})" if u.get("username") else ""
        lines.append(f"{icon} {html.escape(u.get('name') or '—')}{uname} — <code>{uid}</code>")
    for c in chunk_lines(lines):
        await update.message.reply_text(c, parse_mode=ParseMode.HTML)


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip().lower()
    if not query:
        await update.message.reply_text("Usage: /find <name | @username | id | words from a message>")
        return
    hits = []
    for uid, u in db["users"].items():
        if query.lstrip("@") in f"{uid} {u.get('name', '')} {u.get('username', '')}".lower():
            hits.append(uid)
    if not hits:
        for uid, thread in db["threads"].items():
            if any(query in m["text"].lower() for m in thread):
                hits.append(uid)
    if not hits:
        await update.message.reply_text("Nothing found.")
        return
    buttons = [[InlineKeyboardButton(user_label(int(uid))[:50], callback_data=f"view_{uid}")] for uid in hits[:10]]
    await update.message.reply_text(f"🔎 {len(hits)} match(es):", reply_markup=InlineKeyboardMarkup(buttons))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db["users"]
    today = datetime.now(TZ).date()
    total_msgs = today_msgs = 0
    for thread in db["threads"].values():
        total_msgs += len(thread)
        for m in thread:
            try:
                if to_local(m["time"]).date() == today:
                    today_msgs += 1
            except Exception:
                pass
    open_n = sum(1 for u in users.values() if u.get("status", "open") == "open")
    banned = sum(1 for u in users.values() if u.get("banned"))
    stars = [r["stars"] for r in db["ratings"]]
    avg = f"{sum(stars) / len(stars):.1f}/5 ({len(stars)} ratings)" if stars else "no ratings yet"
    up = int(time.time() - START_TIME)
    await update.message.reply_text(
        "📊 Stats\n\n"
        f"👥 Users: {len(users)} (🚫 {banned} banned)\n"
        f"📥 Open tickets: {open_n}\n"
        f"💬 Messages: {total_msgs} total · {today_msgs} today\n"
        f"⭐ Satisfaction: {avg}\n"
        f"⏱ Uptime: {up // 3600}h {(up % 3600) // 60}m"
    )


async def _set_ban(update: Update, context: ContextTypes.DEFAULT_TYPE, value: bool):
    if not context.args or not context.args[0].isdigit() or context.args[0] not in db["users"]:
        await update.message.reply_text("Usage: /ban <user_id> (user must have messaged the bot)")
        return
    db["users"][context.args[0]]["banned"] = value
    mark_dirty()
    await update.message.reply_text(
        f"{'🚫 Banned' if value else '♻️ Unbanned'} {user_label(int(context.args[0]))}"
    )


async def ban_command(update, context):
    await _set_ban(update, context, True)


async def unban_command(update, context):
    await _set_ban(update, context, False)


async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            f"Current welcome message:\n\n{db['settings']['welcome']}\n\n"
            "Change: /setwelcome <new text>   Reset: /setwelcome reset"
        )
        return
    db["settings"]["welcome"] = DEFAULT_WELCOME if text.lower() == "reset" else text
    mark_dirty()
    await update.message.reply_text("✅ Welcome message updated.")


# ---------- broadcast ----------

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    n = sum(1 for u in db["users"].values() if not u.get("banned"))
    context.bot_data["pending_broadcast"] = text
    await update.message.reply_text(
        f"📣 Preview:\n\n{text}\n\nSend to {n} user(s)?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Send", callback_data="bc_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="bc_no"),
        ]]),
    )


async def handle_broadcast_choice(q, context, choice: str):
    text = context.bot_data.pop("pending_broadcast", None)
    if choice != "yes" or not text:
        await q.answer("Cancelled")
        await q.edit_message_text("Broadcast cancelled.")
        return
    await q.answer("Sending…")
    await q.edit_message_text("📤 Broadcast started… I'll report when done.")
    targets = [int(uid) for uid, u in db["users"].items() if not u.get("banned")]
    spawn(run_broadcast(context.bot, text, targets))


async def run_broadcast(bot, text: str, targets: list):
    ok = blocked = failed = 0
    for uid in targets:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(uid, text)
                ok += 1
            except TelegramError:
                failed += 1
        except Forbidden:
            blocked += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await bot.send_message(
        ADMIN_ID, f"📣 Broadcast done.\n✅ Delivered: {ok}\n🚫 Blocked bot: {blocked}\n⚠️ Failed: {failed}"
    )


# ---------- backup / restore ----------

async def send_backup(bot, silent: bool = False, note: str = ""):
    flush(force=True)
    raw = json.dumps(db, ensure_ascii=False, indent=1).encode("utf-8")
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M")
    await bot.send_document(
        ADMIN_ID,
        document=BytesIO(raw),
        filename=f"support_backup_{stamp}.json",
        caption=f"💾 Backup {note}\nAfter a redeploy: send this file back with caption /restore",
        disable_notification=silent,
    )


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_backup(context.bot, note="(manual)")


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.effective_message.document
    if not doc or not (doc.file_name or "").endswith(".json"):
        await update.effective_message.reply_text("Send the .json backup file with caption /restore")
        return
    try:
        f = await doc.get_file()
        data = json.loads(bytes(await f.download_as_bytearray()).decode("utf-8"))
        if not isinstance(data, dict) or "users" not in data or "threads" not in data:
            raise ValueError("not a support-bot backup")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Invalid backup: {e}")
        return
    _normalize(data)
    db.clear()
    db.update(data)
    mark_dirty()
    flush(force=True)
    await update.effective_message.reply_text(
        f"✅ Restored: {len(db['users'])} users, "
        f"{sum(len(t) for t in db['threads'].values())} messages."
    )


async def restore_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("To restore: send the .json backup file with the caption /restore")


# ============================== BACKGROUND JOBS ==============================

class HealthHandler(BaseHTTPRequestHandler):
    """Tiny web server so Render sees an open port and UptimeRobot can ping."""

    def _respond(self, head: bool = False):
        body = json.dumps({"status": "ok", "uptime_s": int(time.time() - START_TIME)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_HEAD(self):
        self._respond(head=True)

    def log_message(self, *args):
        pass


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    logger.info(f"Health server listening on :{PORT}")


async def autosave_loop():
    while True:
        await asyncio.sleep(5)
        try:
            flush()
        except Exception as e:
            logger.error(f"Autosave failed: {e}")


async def keepalive_loop():
    """Backup keep-alive: pings our own public URL (UptimeRobot is the main one)."""
    if not SELF_URL:
        logger.info("No RENDER_EXTERNAL_URL/SELF_URL set - self-ping disabled.")
        return
    url = SELF_URL.rstrip("/") + "/health"
    await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                await client.get(url)
            except Exception as e:
                logger.warning(f"Self-ping failed: {e}")
            await asyncio.sleep(600)  # every 10 min (Render sleeps after 15)


async def backup_loop(bot):
    if BACKUP_HOURS <= 0:
        return
    last = _version
    while True:
        await asyncio.sleep(BACKUP_HOURS * 3600)
        if _version != last and db["users"]:
            try:
                await send_backup(bot, silent=True, note="(auto)")
                last = _version
            except TelegramError as e:
                logger.warning(f"Auto-backup failed: {e}")


_last_error_notice = 0.0


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    global _last_error_notice
    err = context.error
    logger.error("Unhandled error", exc_info=err)
    if isinstance(err, (NetworkError, Conflict)):
        return  # transient (e.g. old instance still shutting down during redeploy)
    if time.time() - _last_error_notice > 600:
        _last_error_notice = time.time()
        try:
            await context.bot.send_message(ADMIN_ID, f"⚠️ Bot error: {type(err).__name__}: {str(err)[:200]}")
        except TelegramError:
            pass


# ============================== LIFECYCLE ==============================

async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "Start"), BotCommand("help", "How to reach us")])
    await app.bot.set_my_commands(
        [
            BotCommand("open", "Tickets waiting for you"),
            BotCommand("chat", "View a user's chat: /chat <id>"),
            BotCommand("find", "Search users/messages"),
            BotCommand("users", "Recent users"),
            BotCommand("stats", "Numbers at a glance"),
            BotCommand("broadcast", "Message all users"),
            BotCommand("backup", "Download data backup"),
            BotCommand("help", "All admin commands"),
        ],
        scope=BotCommandScopeChat(ADMIN_ID),
    )
    spawn(autosave_loop())
    spawn(keepalive_loop())
    spawn(backup_loop(app.bot))
    if _fresh_start:
        try:
            await app.bot.send_message(
                ADMIN_ID,
                "🚀 Bot started with an empty database.\n"
                "If this was a redeploy, send your latest backup file with caption /restore.",
            )
        except TelegramError:
            pass
    logger.info("Support bot is up.")


async def post_shutdown(app: Application):
    for t in list(_tasks):
        t.cancel()
    flush(force=True)
    logger.info("Data flushed, bye.")


def main():
    start_health_server()  # open the port first so Render's health check passes

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    admin = filters.User(ADMIN_ID)
    private_msg = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE & ~filters.StatusUpdate.ALL

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("chat", chat_command, filters=admin))
    app.add_handler(CommandHandler("open", open_command, filters=admin))
    app.add_handler(CommandHandler("users", users_command, filters=admin))
    app.add_handler(CommandHandler("find", find_command, filters=admin))
    app.add_handler(CommandHandler("stats", stats_command, filters=admin))
    app.add_handler(CommandHandler("ban", ban_command, filters=admin))
    app.add_handler(CommandHandler("unban", unban_command, filters=admin))
    app.add_handler(CommandHandler("broadcast", broadcast_command, filters=admin))
    app.add_handler(CommandHandler("setwelcome", setwelcome_command, filters=admin))
    app.add_handler(CommandHandler("backup", backup_command, filters=admin))
    app.add_handler(CommandHandler("restore", restore_hint, filters=admin))
    app.add_handler(CallbackQueryHandler(on_callback))

    # Admin: backup restore, then everything else = reply to a user
    app.add_handler(MessageHandler(
        admin & filters.Document.ALL & filters.CaptionRegex(r"^/restore"), restore_command))
    app.add_handler(MessageHandler(private_msg & admin & ~filters.COMMAND, handle_admin_message))
    # Users: any message type
    app.add_handler(MessageHandler(private_msg & ~admin & ~filters.COMMAND, handle_user_message))

    app.add_error_handler(on_error)

    logger.info("Support bot starting...")
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()
