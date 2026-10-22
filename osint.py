
import os
import time
import html
import sqlite3
import logging
import threading

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============ CONFIG ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5989928539"))
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "forwarder.db")

ADD_CHANNEL_URL = (
    "https://t.me/Join_request_acceptbot"
    "?startchannel=true"
    "&admin=post_messages+delete_messages+edit_messages+invite_users+promote_members"
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("forwarder")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)


# ============ DATABASE ============
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    conn = db()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sources (
            user_id INTEGER,
            chat_id INTEGER,
            title TEXT,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS destinations (
            user_id INTEGER,
            chat_id INTEGER,
            title TEXT,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS admin_broadcast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0
        );
        """)
        conn.commit()
    finally:
        conn.close()


def save_user(user):
    conn = db()
    try:
        conn.execute("""
            INSERT INTO users(user_id, first_name)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name
        """, (user.id, user.first_name or "User"))
        conn.commit()
    finally:
        conn.close()


def add_chat(table, user_id, chat_id, title):
    if table not in ("sources", "destinations"):
        raise ValueError("Invalid table")

    conn = db()
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {table}(user_id, chat_id, title) "
            "VALUES (?, ?, ?)",
            (user_id, chat_id, title or str(chat_id))
        )
        conn.commit()
    finally:
        conn.close()


def get_chats(table, user_id):
    if table not in ("sources", "destinations"):
        raise ValueError("Invalid table")

    conn = db()
    try:
        return conn.execute(
            f"SELECT chat_id, title FROM {table} "
            "WHERE user_id=? ORDER BY title",
            (user_id,)
        ).fetchall()
    finally:
        conn.close()


def remove_chat(table, user_id, chat_id):
    if table not in ("sources", "destinations"):
        raise ValueError("Invalid table")

    conn = db()
    try:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_all_user_ids():
    conn = db()
    try:
        return [
            row[0] for row in conn.execute(
                "SELECT user_id FROM users"
            ).fetchall()
        ]
    finally:
        conn.close()


def get_source_owners(chat_id):
    conn = db()
    try:
        return [
            row[0] for row in conn.execute(
                "SELECT user_id FROM sources WHERE chat_id=?",
                (chat_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


# ============ KEYBOARDS ============
def main_menu(user_id):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(
            "➕ Add Source Channel",
            url=ADD_CHANNEL_URL
        ),
        InlineKeyboardButton(
            "➕ Add Destination Channel / Group",
            url=ADD_CHANNEL_URL
        ),
        InlineKeyboardButton(
            "⚙️ My Forwarding Settings",
            callback_data="settings"
        ),
        InlineKeyboardButton(
            "📋 My Channels",
            callback_data="my_chats"
        )
    )

    if user_id == ADMIN_ID:
        markup.add(
            InlineKeyboardButton(
                "📣 Admin Broadcast",
                callback_data="admin_broadcast"
            )
        )
    return markup


def settings_menu():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📥 Set Source by ID", callback_data="set_source"),
        InlineKeyboardButton("📤 Add Destination by ID", callback_data="add_dest"),
        InlineKeyboardButton("🗑 Remove Source by ID", callback_data="remove_source"),
        InlineKeyboardButton("🗑 Remove Destination by ID", callback_data="remove_dest"),
        InlineKeyboardButton("⬅️ Main Menu", callback_data="home")
    )
    return markup


# ============ PENDING INPUTS ============
pending = {}
pending_lock = threading.Lock()


def ask_for_id(call, action):
    with pending_lock:
        pending[call.from_user.id] = action

    bot.send_message(
        call.message.chat.id,
        "Chat ID bhejein.\n\n"
        "Channel ID aksar -100 se shuru hoti hai.\n"
        "Bot ko us channel/group mein add karein aur required posting permissions dein.\n\n"
        "Cancel karne ke liye /cancel bhejein."
    )


@bot.message_handler(commands=["cancel"])
def cancel_input(message):
    with pending_lock:
        pending.pop(message.from_user.id, None)
    bot.reply_to(message, "❎ Cancelled.")


# ============ START ============
@bot.message_handler(commands=["start"])
def start(message):
    save_user(message.from_user)

    bot.send_message(
        message.chat.id,
        "🤖 <b>Multi-User Auto Forward Bot</b>\n\n"
        "Har user apne source aur destination channels alag configure kar sakta hai.\n\n"
        "1. Source channel set karein.\n"
        "2. Destination channels/groups add karein.\n"
        "3. Source mein aane wali posts automatic forward hongi.\n\n"
        "Chat IDs manually register karne ke liye Settings kholein.",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


# ============ CALLBACKS ============
@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    uid = call.from_user.id

    if call.data == "home":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🏠 Main Menu",
            reply_markup=main_menu(uid)
        )

    elif call.data == "settings":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⚙️ <b>Forwarding Settings</b>",
            reply_markup=settings_menu(),
            parse_mode="HTML"
        )

    elif call.data == "set_source":
        bot.answer_callback_query(call.id)
        ask_for_id(call, "set_source")

    elif call.data == "add_dest":
        bot.answer_callback_query(call.id)
        ask_for_id(call, "add_dest")

    elif call.data == "remove_source":
        bot.answer_callback_query(call.id)
        ask_for_id(call, "remove_source")

    elif call.data == "remove_dest":
        bot.answer_callback_query(call.id)
        ask_for_id(call, "remove_dest")

    elif call.data == "my_chats":
        bot.answer_callback_query(call.id)
        sources = get_chats("sources", uid)
        destinations = get_chats("destinations", uid)

        lines = ["📋 <b>Your Channels</b>", "", "📥 <b>Sources:</b>"]
        lines += [
            f"• {html.escape(title)} — <code>{chat_id}</code>"
            for chat_id, title in sources
        ] or ["• None"]

        lines += ["", "📤 <b>Destinations:</b>"]
        lines += [
            f"• {html.escape(title)} — <code>{chat_id}</code>"
            for chat_id, title in destinations
        ] or ["• None"]

        bot.send_message(
            call.message.chat.id,
            "\n".join(lines),
            parse_mode="HTML"
        )

    elif call.data == "admin_broadcast":
        bot.answer_callback_query(call.id)

        if uid != ADMIN_ID:
            bot.send_message(call.message.chat.id, "⛔ Not authorized.")
            return

        with pending_lock:
            pending[uid] = "admin_broadcast"

        bot.send_message(
            call.message.chat.id,
            "📣 Admin Broadcast\n\n"
            "Ab text, photo, document ya doosra supported message bhejein.\n"
            "Bot us message ko apne registered users ko copy karega.\n"
            "Cancel: /cancel"
        )

    else:
        bot.answer_callback_query(call.id, "Unknown action")


# ============ RECEIVE IDs / BROADCAST ============
@bot.message_handler(
    content_types=["text", "photo", "video", "document", "audio",
                   "animation", "voice", "video_note", "sticker"]
)
def handle_input(message):
    uid = message.from_user.id

    with pending_lock:
        action = pending.get(uid)

    if not action:
        return

    if action == "admin_broadcast":
        if uid != ADMIN_ID:
            with pending_lock:
                pending.pop(uid, None)
            return

        # Avoid treating commands as broadcast content.
        if message.content_type == "text" and (message.text or "").startswith("/"):
            return

        with pending_lock:
            pending.pop(uid, None)

        sent = 0
        failed = 0

        for target_id in get_all_user_ids():
            try:
                bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                sent += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Broadcast failed for %s: %s",
                    target_id, exc
                )
            time.sleep(0.04)

        conn = db()
        try:
            conn.execute(
                "INSERT INTO admin_broadcast_log(sent, failed) VALUES (?, ?)",
                (sent, failed)
            )
            conn.commit()
        finally:
            conn.close()

        bot.send_message(
            message.chat.id,
            f"📣 <b>Broadcast finished</b>\n"
            f"✅ Sent: {sent}\n"
            f"⚠️ Failed: {failed}",
            parse_mode="HTML"
        )
        return

    # Chat ID entry must be text.
    if message.content_type != "text":
        bot.reply_to(message, "⚠️ Please send a numeric chat ID.")
        return

    try:
        chat_id = int(message.text.strip())
    except (TypeError, ValueError):
        bot.reply_to(message, "⚠️ Invalid ID. Numeric chat ID bhejein.")
        return

    if action in ("set_source", "add_dest"):
        try:
            chat = bot.get_chat(chat_id)
            member = bot.get_chat_member(chat_id, bot.get_me().id)

            # A source channel must permit bot to receive channel posts.
            if member.status not in ("administrator", "creator"):
                bot.reply_to(
                    message,
                    "⚠️ Bot ko pehle us channel/group mein admin banayein."
                )
                return

            if action == "set_source":
                add_chat("sources", uid, chat_id, chat.title or str(chat_id))
                response = f"✅ Source saved: {html.escape(chat.title or str(chat_id))}"
            else:
                add_chat("destinations", uid, chat_id, chat.title or str(chat_id))
                response = f"✅ Destination saved: {html.escape(chat.title or str(chat_id))}"

            with pending_lock:
                pending.pop(uid, None)

            bot.send_message(
                message.chat.id,
                response,
                parse_mode="HTML"
            )

        except Exception:
            logger.exception("Chat registration failed")
            bot.reply_to(
                message,
                "❌ Chat verify nahi ho paya. Check karein ki bot us chat mein "
                "added/admin hai aur ID sahi hai."
            )

    elif action in ("remove_source", "remove_dest"):
        table = "sources" if action == "remove_source" else "destinations"
        removed = remove_chat(table, uid, chat_id)

        with pending_lock:
            pending.pop(uid, None)

        bot.reply_to(
            message,
            "✅ Removed." if removed else "⚠️ ID aapki list mein nahi mila."
        )


# ============ AUTOMATIC FORWARDING ============
@bot.channel_post_handler(
    content_types=["text", "photo", "video", "document", "audio",
                   "animation", "voice", "video_note", "sticker"]
)
def forward_channel_post(message):
    source_id = message.chat.id
    owners = get_source_owners(source_id)

    if not owners:
        return

    for owner_id in owners:
        destinations = get_chats("destinations", owner_id)

        for dest_id, title in destinations:
            try:
                bot.forward_message(
                    chat_id=dest_id,
                    from_chat_id=source_id,
                    message_id=message.message_id
                )
                logger.info(
                    "Forwarded %s -> %s for user %s",
                    source_id, dest_id, owner_id
                )
            except Exception:
                logger.exception(
                    "Forward failed: %s -> %s",
                    source_id, dest_id
                )


# ============ HEALTH SERVER ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        body = b"Bot is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(
        target=server.serve_forever,
        daemon=True
    ).start()


# ============ MAIN ============
def main():
    init_db()
    start_health_server()

    logger.info("Auto Forward Bot starting")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=25,
                allowed_updates=[
                    "message",
                    "channel_post",
                    "callback_query"
                ],
                skip_pending=False
            )
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("Polling crashed; restarting in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
